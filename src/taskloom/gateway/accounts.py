import asyncio
import hashlib
import hmac
import secrets

from taskloom.contracts.models import (
    AccountCredential,
    AccountLogin,
    AccountRecord,
    APIKeyRecord,
    PlatformConfig,
    identity,
)
from taskloom.domain.model import DomainError
from taskloom.domain.ports import Repository, StoredRecord


def password_hash(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    value = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 260000)
    return salt + "$" + value.hex()


class AccountService:
    """Platform-owned accounts and revocable keys; feedback excludes secrets."""

    def __init__(self, store: Repository, config: PlatformConfig) -> None:
        self.store = store
        self.config = config
        self.accounts: dict[str, AccountRecord] = {}
        self.keys: dict[str, APIKeyRecord] = {}
        self.platform_id = ""
        self.lock = asyncio.Lock()
        self.hash_slots = asyncio.Semaphore(4)

    async def start(self) -> None:
        metadata = await self.store.get("platform", "identity")
        if metadata is None:
            metadata = {"platform_id": identity()}
            await self.store.apply([StoredRecord("platform", "identity", "_platform", metadata)])
        self.platform_id = metadata["platform_id"]
        for data in await self.store.records("account"):
            account = AccountRecord.model_validate(data)
            self.accounts[account.account_id] = account
        for data in await self.store.records("api_key"):
            key = APIKeyRecord.model_validate(data)
            self.keys[key.digest] = key

    def get(self, account_id: str) -> AccountRecord:
        if account_id not in self.accounts:
            raise DomainError("UNAUTHORIZED", "A registered platform account is required")
        return self.accounts[account_id]

    def authenticate(self, api_key: str) -> str:
        fingerprint = hashlib.sha256(api_key.encode()).hexdigest()
        key = self.keys.get(fingerprint)
        if key is None or key.revoked:
            raise DomainError("UNAUTHORIZED", "Invalid or revoked platform API key")
        self.get(key.account_id)
        return key.account_id

    async def register(self, login: AccountLogin) -> AccountCredential:
        async with self.hash_slots:
            verifier = await asyncio.to_thread(password_hash, login.password)
        async with self.lock:
            if login.username in self.accounts:
                raise DomainError("ACCOUNT_CONFLICT", "Platform username is already registered")
            if len(self.accounts) >= 1024:
                raise DomainError("ACCOUNT_CAPACITY", "Platform account capacity exhausted")
            account = AccountRecord(
                account_id=login.username,
                password_hash=verifier,
                budget_units=self.config.account_budget_units,
                max_inflight=self.config.account_max_inflight,
                requests_per_window=self.config.account_requests_per_window,
                tokens_per_window=self.config.account_tokens_per_window,
            )
            credential, key = self._new_key(account.account_id)
            await self.store.apply(
                [
                    StoredRecord(
                        "account", account.account_id, account.account_id, account.model_dump()
                    ),
                    StoredRecord("api_key", key.key_id, key.account_id, key.model_dump()),
                ]
            )
            self.accounts[account.account_id] = account
            self.keys[key.digest] = key
            return credential

    def _new_key(self, account_id: str) -> tuple[AccountCredential, APIKeyRecord]:
        key_id = identity()
        secret = "tlk_" + secrets.token_urlsafe(32)
        key = APIKeyRecord(
            key_id=key_id,
            account_id=account_id,
            digest=hashlib.sha256(secret.encode()).hexdigest(),
        )
        return AccountCredential(account_id=account_id, key_id=key_id, api_key=secret), key

    async def login(self, login: AccountLogin) -> AccountCredential:
        account = self.accounts.get(login.username)
        if account is None:
            raise DomainError("UNAUTHORIZED", "Invalid platform username or password")
        salt = account.password_hash.split("$", 1)[0]
        async with self.hash_slots:
            actual = await asyncio.to_thread(password_hash, login.password, salt)
        if not hmac.compare_digest(actual, account.password_hash):
            raise DomainError("UNAUTHORIZED", "Invalid platform username or password")
        async with self.lock:
            active = [
                k
                for k in self.keys.values()
                if k.account_id == account.account_id and not k.revoked
            ]
            if len(active) >= 8:
                raise DomainError(
                    "KEY_LIMIT", "Revoke an existing account key before issuing another"
                )
            credential, key = self._new_key(account.account_id)
            await self.store.apply(
                [StoredRecord("api_key", key.key_id, key.account_id, key.model_dump())]
            )
            self.keys[key.digest] = key
            return credential

    async def revoke(self, account_id: str, key_id: str) -> None:
        async with self.lock:
            for fingerprint, key in self.keys.items():
                if key.key_id == key_id and key.account_id == account_id:
                    revoked = key.model_copy(update={"revoked": True})
                    await self.store.apply(
                        [StoredRecord("api_key", key_id, account_id, revoked.model_dump())]
                    )
                    self.keys[fingerprint] = revoked
                    return
            raise DomainError("NOT_FOUND", "Account API key not found")
