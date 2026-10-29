"""Coordination for shared initialization and file access between tool attempts.

One coordinator serves one client profile and is the only issuer of access
permits. It gives the executor three things:

* a normalized resource key, so two attempts that ask for the same read-only
  preparation of the same staged inputs point at one artifact;
* a single-flight claim, so the real initialization runs once while the other
  attempts wait for the published outcome and reuse it;
* ordered resource locks plus a ledger, so readers run in parallel, a writer
  runs alone, lock waits and shared ownership are recorded, and the execution
  cost of a shared preparation is charged once.

Tool processes are separate OS processes, so the coordinator hands each of them
a signed permit; the process verifies the permit and then takes advisory file
locks in the same canonical order before it touches the workspace.
"""

import asyncio
import fcntl
import hmac
import json
import os
import time
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Literal

from taskloom.contracts.models import JsonObject, ToolRun
from taskloom.domain.identity import digest
from taskloom.policies.access import KeyedMutex, ReadWriteLock, SingleFlight

AccessMode = Literal["read", "write"]
LEDGER_NAME = "ledger.jsonl"
SECRET_NAME = "secret"
SHARED_NAME = "shared"
LOCKS_NAME = "locks"


@dataclass(frozen=True)
class ResourceKey:
    """Normalized identity of one shared read-only preparation.

    The staged inputs are pinned by ``input_version``; the tool name, the
    arguments and the work factor complete the identity because they decide
    what the preparation produces.
    """

    input_version: str
    tool_name: str
    arguments_digest: str
    work_iterations: int

    @classmethod
    def of(cls, request: ToolRun) -> "ResourceKey":
        """Build the key of one tool request.

        Args:
            request: Tool request whose outcome may be shared.

        Returns:
            The normalized resource key of that request.
        """
        return cls(
            input_version=request.input_version,
            tool_name=request.tool_name,
            arguments_digest=digest(request.arguments),
            work_iterations=request.work_iterations,
        )

    @property
    def value(self) -> str:
        """Return the full identity of the resource, stable across processes."""
        return digest(
            {
                "input_version": self.input_version,
                "tool_name": self.tool_name,
                "arguments": self.arguments_digest,
                "work_iterations": self.work_iterations,
            }
        )

    @property
    def slug(self) -> str:
        """Return the short file-name form of the resource identity."""
        return sha256(self.value.encode()).hexdigest()[:32]


@dataclass(frozen=True)
class AccessPermit:
    """Proof that the coordinator admitted one attempt for one resource."""

    permit_id: str
    resource: str
    holder: str
    owner: str
    mode: AccessMode
    input_version: str
    issued_at: float
    expires_at: float
    signature: str

    def body(self) -> dict[str, Any]:
        """Return the signed part of the permit."""
        return {
            "permit_id": self.permit_id,
            "resource": self.resource,
            "holder": self.holder,
            "owner": self.owner,
            "mode": self.mode,
            "input_version": self.input_version,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }


def sign_permit(secret: bytes, body: dict[str, Any]) -> str:
    """Sign a permit body.

    Args:
        secret: Per-profile secret shared with the tool processes.
        body: Signed part of the permit.

    Returns:
        Hex HMAC-SHA256 digest of the canonical body.
    """
    message = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hmac.new(secret, message.encode(), sha256).hexdigest()


def check_permit(
    permit: AccessPermit,
    secret: bytes,
    *,
    holder: str | None = None,
    input_version: str | None = None,
    now: float | None = None,
) -> None:
    """Reject a permit that is forged, expired or aimed at another attempt.

    Args:
        permit: Permit handed to the tool process.
        secret: Per-profile secret shared with the coordinator.
        holder: Attempt id the permit must name.
        input_version: Staged input version the permit must name.
        now: Wall clock to check the validity window against.

    Raises:
        ValueError: The permit is forged, expired, or made out to other terms.
    """
    if not hmac.compare_digest(sign_permit(secret, permit.body()), permit.signature):
        raise ValueError("Access permit signature does not match")
    if (now if now is not None else time.time()) >= permit.expires_at:
        raise ValueError("Access permit has expired")
    if holder is not None and permit.holder != holder:
        raise ValueError("Access permit belongs to another attempt")
    if input_version is not None and permit.input_version != input_version:
        raise ValueError("Access permit input version differs")


@dataclass(frozen=True)
class PublishedOutcome:
    """A shared preparation plus the bookkeeping that proves how it was shared."""

    payload: JsonObject
    version: str
    owner: str
    published_at: float
    expires_at: float
    users: int


class FileLease:
    """Advisory locks held by one tool process for the length of one attempt.

    Locks are always taken in sorted path order, so two attempts that need the
    same resources in opposite order can never deadlock. A reader takes a
    shared lock, a writer takes an exclusive one.
    """

    def __init__(self, locks: Sequence[tuple[Path, AccessMode]]) -> None:
        """Record the resources to lock.

        Args:
            locks: Resource lock files paired with the access mode.
        """
        self._locks = sorted(locks, key=lambda item: str(item[0]))
        self._handles: list[IO[bytes]] = []
        self.waited_ms = 0.0

    def __enter__(self) -> "FileLease":
        started = time.monotonic()
        try:
            for path, mode in self._locks:
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = path.open("ab")
                operation = fcntl.LOCK_SH if mode == "read" else fcntl.LOCK_EX
                fcntl.flock(handle.fileno(), operation)
                self._handles.append(handle)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        self.waited_ms = round((time.monotonic() - started) * 1000, 3)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        while self._handles:
            handle = self._handles.pop()
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


class AccessCoordinator:
    """One coordinator per client profile; the only issuer of access permits."""

    def __init__(
        self,
        directory: Path,
        *,
        permit_ttl: float = 300.0,
        share_ttl: float = 600.0,
    ) -> None:
        """Open the coordinator state directory.

        Args:
            directory: Directory that holds the secret, the shared artifacts,
                the lock files and the ledger.
            permit_ttl: Seconds an issued permit stays valid.
            share_ttl: Seconds a published preparation may be reused.
        """
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.permit_ttl = permit_ttl
        self.share_ttl = share_ttl
        self.secret_path = self.directory / SECRET_NAME
        self.shared_directory = self.directory / SHARED_NAME
        self.shared_directory.mkdir(parents=True, exist_ok=True)
        self.locks_directory = self.directory / LOCKS_NAME
        self.locks_directory.mkdir(parents=True, exist_ok=True)
        self.ledger_path = self.directory / LEDGER_NAME
        self._secret = self._load_secret()
        self._flight = SingleFlight()
        self._mutex = KeyedMutex()
        self._locks: dict[str, ReadWriteLock] = {}
        self._inflight: dict[str, str] = {}
        self._ready: dict[str, asyncio.Future[PublishedOutcome]] = {}
        self._published: dict[str, PublishedOutcome] = {}
        self._failed: dict[str, str] = {}
        self._users: dict[str, set[str]] = defaultdict(set)

    # ------------------------------------------------------------------ permits

    def _load_secret(self) -> bytes:
        if self.secret_path.exists():
            return self.secret_path.read_bytes()
        secret = os.urandom(32)
        descriptor = os.open(self.secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(secret)
        return secret

    @property
    def secret(self) -> bytes:
        """Return the per-profile permit secret."""
        return self._secret

    def issue_permit(
        self,
        key: ResourceKey,
        *,
        holder: str,
        owner: str,
        mode: AccessMode,
        input_version: str,
    ) -> AccessPermit:
        """Admit one attempt for one resource and return its proof.

        Args:
            key: Resource the attempt wants to touch.
            holder: Attempt id that will carry the permit.
            owner: Attempt id that carries the execution cost.
            mode: Read or write access.
            input_version: Staged input version the permit is valid for.

        Returns:
            A signed permit for that attempt.
        """
        now = time.time()
        body: dict[str, Any] = {
            "permit_id": uuid.uuid4().hex,
            "resource": key.value,
            "holder": holder,
            "owner": owner,
            "mode": mode,
            "input_version": input_version,
            "issued_at": now,
            "expires_at": now + self.permit_ttl,
        }
        permit = AccessPermit(**body, signature=sign_permit(self._secret, body))
        self.record(
            "permit_issued",
            resource=key.value,
            permit_id=permit.permit_id,
            holder=holder,
            owner=owner,
            mode=mode,
        )
        return permit

    def lock_file(self, name: str) -> Path:
        """Return the advisory lock file of one resource name."""
        return self.locks_directory / (sha256(name.encode()).hexdigest()[:32] + ".lock")

    # ----------------------------------------------------------- shared outcome

    def claim(self, key: ResourceKey, holder: str) -> bool:
        """Register the attempt that will carry the initialization cost.

        Args:
            key: Resource to prepare.
            holder: Attempt id that asks for it.

        Returns:
            ``True`` when this attempt owns the preparation, ``False`` when
            another attempt is already preparing the same resource.
        """
        self._users[key.value].add(holder)
        if key.value in self._inflight:
            return False
        self._inflight[key.value] = holder
        self._failed.pop(key.value, None)
        return True

    def release(self, key: ResourceKey) -> None:
        """Drop the in-flight claim of one resource without publishing."""
        self._inflight.pop(key.value, None)

    async def await_outcome(self, key: ResourceKey) -> PublishedOutcome:
        """Wait for the shared outcome; concurrent waiters share one wait.

        Args:
            key: Resource whose published outcome is awaited.

        Returns:
            The published outcome once the owner finished it.
        """

        async def wait() -> PublishedOutcome:
            published = await self.lookup(key)
            if published is not None:
                return published
            failed = self._failed.get(key.value)
            if failed is not None:
                raise RuntimeError(failed)
            # Registering the future runs without an await after the checks, so
            # a publish/fail that happens next always finds this future.
            future = self._ready.get(key.value)
            if future is None:
                future = asyncio.get_running_loop().create_future()
                self._ready[key.value] = future
            return await asyncio.shield(future)

        return await self._flight.run(key.value, wait)

    async def publish(self, key: ResourceKey, payload: JsonObject, owner: str) -> PublishedOutcome:
        """Publish the shared artifact and hand it to every waiter.

        Args:
            key: Resource that was prepared.
            payload: Read-only result that other attempts may reuse.
            owner: Attempt id that executed the preparation.

        Returns:
            The published outcome, including how many attempts used it.
        """
        now = time.time()
        outcome = PublishedOutcome(
            payload=json.loads(json.dumps(payload)),
            version=digest(payload),
            owner=owner,
            published_at=now,
            expires_at=now + self.share_ttl,
            users=len(self._users[key.value]),
        )
        async with self.resources([self.shared_name(key)], "write"):
            self._published[key.value] = outcome
            self._failed.pop(key.value, None)
            self._write_shared(key, outcome)
            self._inflight.pop(key.value, None)
            future = self._ready.pop(key.value, None)
            if future is not None and not future.done():
                future.set_result(outcome)
        self.record(
            "shared_published",
            resource=key.value,
            owner=owner,
            version=outcome.version,
            users=outcome.users,
        )
        return outcome

    async def fail(self, key: ResourceKey, error: str) -> None:
        """Report a failed initialization so waiters stop and may apply again.

        Args:
            key: Resource whose preparation failed.
            error: Message handed to the waiting attempts.
        """
        async with self.resources([self.shared_name(key)], "write"):
            self._inflight.pop(key.value, None)
            self._failed[key.value] = error
            future = self._ready.pop(key.value, None)
            if future is not None and not future.done():
                future.set_exception(RuntimeError(error))
        self.record("shared_failed", resource=key.value, error=error)

    async def lookup(self, key: ResourceKey) -> PublishedOutcome | None:
        """Return a published outcome that is still inside its validity window.

        Args:
            key: Resource to look up.

        Returns:
            The reusable outcome, or ``None`` when nothing valid is published.
        """
        async with self.resources([self.shared_name(key)], "read"):
            outcome = self._published.get(key.value)
            if outcome is None:
                outcome = self._read_shared(key)
            if outcome is not None and outcome.expires_at <= time.time():
                return None
            return outcome

    def users(self, key: ResourceKey) -> int:
        """Return how many attempts used one shared preparation."""
        return len(self._users[key.value])

    # -------------------------------------------------------------- file access

    @staticmethod
    def shared_name(key: ResourceKey) -> str:
        """Return the lock name of the shared artifact of one resource."""
        return "shared:" + key.slug

    @staticmethod
    def workspace_name(root_task_id: str) -> str:
        """Return the lock name of one staged task workspace."""
        return "workspace:" + root_task_id

    def tool_locks(self, key: ResourceKey, root_task_id: str) -> list[tuple[Path, AccessMode]]:
        """Return the lock files a tool process must hold while it works.

        Args:
            key: Resource the tool prepares.
            root_task_id: Task whose staged inputs the tool reads.

        Returns:
            Lock files paired with the access mode, in canonical order.
        """
        return [
            (self.lock_file(self.shared_name(key)), "write"),
            (self.lock_file(self.workspace_name(root_task_id)), "read"),
        ]

    @asynccontextmanager
    async def resources(self, keys: Iterable[str], mode: AccessMode) -> AsyncIterator[None]:
        """Take several resource locks in one canonical order.

        Args:
            keys: Resource names that must be held together.
            mode: Read access runs in parallel, write access runs alone.

        Yields:
            Control once every lock is held.
        """
        ordered = sorted(set(keys))
        started = time.monotonic()
        if mode == "write":
            # A writer takes its whole key set as one unit; readers do not need
            # the mutex, so they run side by side on the same resource.
            async with self._mutex.locks(ordered):
                async with AsyncExitStack() as stack:
                    for name in ordered:
                        await stack.enter_async_context(self._resource(name, mode))
                    yield
        else:
            async with AsyncExitStack() as stack:
                for name in ordered:
                    await stack.enter_async_context(self._resource(name, mode))
                yield
        self.record(
            "lock_wait",
            mode=mode,
            waited_ms=round((time.monotonic() - started) * 1000, 3),
            keys=ordered,
        )

    @asynccontextmanager
    async def _resource(self, name: str, mode: AccessMode) -> AsyncIterator[None]:
        lock = self._locks.setdefault(name, ReadWriteLock())
        if mode == "read":
            async with lock.read():
                yield
        else:
            async with lock.write():
                yield

    # ------------------------------------------------------------------- ledger

    def record(self, event: str, **fields: Any) -> None:
        """Append one ledger line.

        Args:
            event: Short event name, for example ``shared_published``.
            **fields: Structured details of that event.
        """
        line = json.dumps({"event": event, "at": time.time(), **fields}, ensure_ascii=False)
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def ledger(self) -> list[dict[str, Any]]:
        """Return every recorded ledger entry in write order."""
        if not self.ledger_path.exists():
            return []
        rows = []
        for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    # ------------------------------------------------------------ shared storage

    def _shared_path(self, key: ResourceKey) -> Path:
        return self.shared_directory / (key.slug + ".json")

    def _write_shared(self, key: ResourceKey, outcome: PublishedOutcome) -> None:
        target = self._shared_path(key)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "resource": key.value,
                    "version": outcome.version,
                    "owner": outcome.owner,
                    "published_at": outcome.published_at,
                    "expires_at": outcome.expires_at,
                    "payload": outcome.payload,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        temporary.replace(target)

    def _read_shared(self, key: ResourceKey) -> PublishedOutcome | None:
        path = self._shared_path(key)
        if not path.exists():
            return None
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return None
        return PublishedOutcome(
            payload=body["payload"],
            version=body["version"],
            owner=body["owner"],
            published_at=body["published_at"],
            expires_at=body["expires_at"],
            users=len(self._users.get(key.value, set())),
        )
