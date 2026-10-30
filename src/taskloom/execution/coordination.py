"""Coordination for shared initialization and file access between tool attempts.

One coordinator serves one client profile and is the only issuer of access
permits. It gives the executor three things:

* a normalized resource key, so two attempts that ask for the same read-only
  preparation of the same staged inputs point at one artifact;
* a single-flight claim, so the real initialization runs once while the other
  attempts wait for the published outcome and reuse it;
* ordered resource locks plus a bounded ledger, so readers run in parallel, a
  writer runs alone (writers are preferred over new readers), lock waits and
  shared ownership are recorded, and the execution cost of a shared
  preparation is charged once.

Only pure tools share: tools whose result is decided by the staged inputs and
the arguments alone, with no side effect, no external path and no approval
(``SHAREABLE_TOOLS``). ``read_file`` and ``run_command`` never share and never
read the shared cache.

Tool processes are separate OS processes, so the coordinator hands each of them
a signed permit; the process verifies the permit and then takes advisory file
locks in the same canonical order before it touches the shared artifact. The
permit is an integrity check against misuse (a tool started without the
executor, or with another attempt's spec), not a security boundary: tool
processes run as the same user and could read the secret themselves.
"""

import asyncio
import fcntl
import hmac
import json
import os
import stat
import time
import uuid
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Literal

from taskloom.contracts.models import JsonObject, ToolRun
from taskloom.domain.identity import digest
from taskloom.domain.model import DomainError
from taskloom.policies.access import CapacityExhausted, ReadWriteLock, SingleFlight

AccessMode = Literal["read", "write"]
LEDGER_NAME = "ledger.jsonl"
SECRET_NAME = "secret"
SHARED_NAME = "shared"
LOCKS_NAME = "locks"
SECRET_BYTES = 32
LEDGER_LIMIT_BYTES = 1048576
MAX_SHARED_ENTRIES = 256

# Tools whose result is decided by the staged inputs and the arguments alone:
# no side effect, no path outside the staged workspace, no approval. Only these
# may share a preparation or be served from the shared cache.
SHAREABLE_TOOLS = frozenset({"file_stats", "grep", "list_files", "top_words"})


class SharedPreparationFailed(RuntimeError):
    """The attempt that owned a shared preparation failed or was cancelled."""


class SharingUnavailable(RuntimeError):
    """The coordinator cannot track one more shared wait; run unshared instead."""


@dataclass(frozen=True)
class ResourceKey:
    """Normalized identity of one shared read-only preparation.

    The staged inputs are pinned by ``input_version``; the account, the
    executor workspace root, the tool name, the arguments, the work factor and
    every approval field complete the identity because each of them can change
    what the tool is allowed to read or what it produces.
    """

    tenant_id: str
    workspace_root: str
    input_version: str
    tool_name: str
    arguments_digest: str
    work_iterations: int
    approved_external_path: str | None
    command_approved: bool
    approved_command_cwd: str | None

    @classmethod
    def of(cls, request: ToolRun, *, workspace_root: str) -> "ResourceKey":
        """Build the key of one tool request.

        Args:
            request: Tool request whose outcome may be shared.
            workspace_root: Resolved workspace root of the serving executor.

        Returns:
            The normalized resource key of that request.
        """
        return cls(
            tenant_id=request.tenant_id,
            workspace_root=workspace_root,
            input_version=request.input_version,
            tool_name=request.tool_name,
            arguments_digest=digest(request.arguments),
            work_iterations=request.work_iterations,
            approved_external_path=request.approved_external_path,
            command_approved=request.command_approved,
            approved_command_cwd=request.approved_command_cwd,
        )

    @property
    def shareable(self) -> bool:
        """Return whether attempts with this key may share one preparation."""
        return (
            self.tool_name in SHAREABLE_TOOLS
            and self.approved_external_path is None
            and not self.command_approved
            and self.approved_command_cwd is None
        )

    @property
    def value(self) -> str:
        """Return the full identity of the resource, stable across processes."""
        return digest(
            {
                "tenant_id": self.tenant_id,
                "workspace_root": self.workspace_root,
                "input_version": self.input_version,
                "tool_name": self.tool_name,
                "arguments": self.arguments_digest,
                "work_iterations": self.work_iterations,
                "approved_external_path": self.approved_external_path,
                "command_approved": self.command_approved,
                "approved_command_cwd": self.approved_command_cwd,
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
        max_entries: int = MAX_SHARED_ENTRIES,
        ledger_limit_bytes: int = LEDGER_LIMIT_BYTES,
    ) -> None:
        """Open the coordinator state directory.

        Args:
            directory: Directory that holds the secret, the shared artifacts,
                the lock files and the ledger.
            permit_ttl: Seconds an issued permit stays valid.
            share_ttl: Seconds a published preparation may be reused; failure
                records are forgotten after the same time.
            max_entries: Most published preparations kept at once; the oldest
                are evicted first.
            ledger_limit_bytes: Size at which the ledger rotates; one rotated
                file is kept, so the ledger stays below twice this size.

        Raises:
            DomainError: ``ACCESS_SECRET_INVALID`` when the permit secret
                exists but cannot be validated or replaced.
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
        self.max_entries = max_entries
        self.ledger_limit_bytes = ledger_limit_bytes
        self._secret = self._load_secret()
        self._flight = SingleFlight()
        self._locks: dict[str, ReadWriteLock] = {}
        self._lock_users: dict[str, int] = {}
        self._inflight: dict[str, str] = {}
        self._ready: dict[str, asyncio.Future[PublishedOutcome]] = {}
        self._published: dict[str, PublishedOutcome] = {}
        self._failed: dict[str, tuple[str, float]] = {}
        self._users: dict[str, set[str]] = {}
        self._sweep_shared()

    # ------------------------------------------------------------------ permits

    def _load_secret(self) -> bytes:
        """Return the permit secret, replacing one that fails validation.

        A valid secret is a regular file of this user, readable by nobody
        else (no group/other bits) and at least ``SECRET_BYTES`` long. A
        secret that fails the check is replaced; permits signed with the old
        one stop verifying, which only affects attempts already in flight
        while the coordinator restarts.
        """
        try:
            info = os.lstat(self.secret_path)
        except FileNotFoundError:
            info = None
        if info is not None:
            if (
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) & 0o077 == 0
                and info.st_size >= SECRET_BYTES
            ):
                return self.secret_path.read_bytes()
            try:
                self.secret_path.unlink()
            except OSError as error:
                raise DomainError(
                    "ACCESS_SECRET_INVALID", "Access permit secret cannot be replaced"
                ) from error
            self.record("secret_replaced", mode=oct(stat.S_IMODE(info.st_mode)), size=info.st_size)
        secret = os.urandom(SECRET_BYTES)
        try:
            descriptor = os.open(self.secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as error:
            raise DomainError(
                "ACCESS_SECRET_INVALID", "Access permit secret cannot be created"
            ) from error
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
        self._users.setdefault(key.value, set()).add(holder)
        if key.value in self._inflight:
            return False
        self._inflight[key.value] = holder
        self._failed.pop(key.value, None)
        return True

    def in_flight(self, key: ResourceKey) -> bool:
        """Return whether some attempt currently prepares this resource."""
        return key.value in self._inflight

    def release(self, key: ResourceKey) -> None:
        """Drop the in-flight claim of one resource without publishing."""
        self._inflight.pop(key.value, None)

    async def await_outcome(self, key: ResourceKey) -> PublishedOutcome:
        """Wait for the shared outcome; concurrent waiters share one wait.

        Args:
            key: Resource whose published outcome is awaited.

        Returns:
            The published outcome once the owner finished it.

        Raises:
            SharedPreparationFailed: The owner failed or was cancelled; the
                key is free to be claimed again.
            SharingUnavailable: Too many distinct shared waits are in flight;
                the caller should run without sharing.
        """

        async def wait() -> PublishedOutcome:
            published = await self.lookup(key)
            if published is not None:
                return published
            failed = self._failed.get(key.value)
            if failed is not None:
                raise SharedPreparationFailed(failed[0])
            # Registering the future runs without an await after the checks, so
            # a publish/fail that happens next always finds this future.
            future = self._ready.get(key.value)
            if future is None:
                future = asyncio.get_running_loop().create_future()
                self._ready[key.value] = future
            return await asyncio.shield(future)

        try:
            return await self._flight.run(key.value, wait)
        except CapacityExhausted as error:
            raise SharingUnavailable(str(error)) from error

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
            users=len(self._users.get(key.value, ())),
        )
        async with self.resources([self.shared_name(key)], "write"):
            self._published[key.value] = outcome
            self._failed.pop(key.value, None)
            self._write_shared(key, outcome)
            self._inflight.pop(key.value, None)
            future = self._ready.pop(key.value, None)
            if future is not None and not future.done():
                future.set_result(outcome)
        self._prune(now)
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
            self._failed[key.value] = (error, time.time())
            future = self._ready.pop(key.value, None)
            if future is not None and not future.done():
                future.set_exception(SharedPreparationFailed(error))
                # Every waiter may have left already; retrieve the error so the
                # loop does not report it as never retrieved.
                future.exception()
        self._prune(time.time())
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
        return len(self._users.get(key.value, ()))

    def _prune(self, now: float) -> None:
        """Bound the in-memory tables and the shared directory.

        Expired publications and their files go first, then the oldest ones
        beyond ``max_entries``; failure records older than ``share_ttl`` are
        forgotten; user sets survive only while their key is still claimed,
        awaited or published.
        """
        expired = [name for name, item in self._published.items() if item.expires_at <= now]
        overflow = len(self._published) - len(expired) - self.max_entries
        if overflow > 0:
            alive = sorted(
                (item.published_at, name)
                for name, item in self._published.items()
                if item.expires_at > now
            )
            expired.extend(name for _, name in alive[:overflow])
        for name in expired:
            self._published.pop(name, None)
            (self.shared_directory / (self._slug(name) + ".json")).unlink(missing_ok=True)
        for name in [name for name, (_, at) in self._failed.items() if at + self.share_ttl <= now]:
            self._failed.pop(name, None)
        keep = set(self._inflight) | set(self._ready) | set(self._published)
        for name in [name for name in self._users if name not in keep]:
            self._users.pop(name, None)

    # -------------------------------------------------------------- file access

    @staticmethod
    def shared_name(key: ResourceKey) -> str:
        """Return the lock name of the shared artifact of one resource."""
        return "shared:" + key.slug

    def tool_locks(self, key: ResourceKey) -> list[tuple[Path, AccessMode]]:
        """Return the lock files a tool process must hold while it works.

        Only a shared preparation is locked: its owner holds the artifact
        exclusively. Staged inputs are not locked; the tool verifies their
        version before and after it reads them instead.

        Args:
            key: Resource the tool prepares.

        Returns:
            Lock files paired with the access mode, in canonical order.
        """
        if not key.shareable:
            return []
        return [(self.lock_file(self.shared_name(key)), "write")]

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
        # Every caller takes its locks in the same sorted order, so two callers
        # that ask for the same set in opposite order cannot deadlock.
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
        self._lock_users[name] = self._lock_users.get(name, 0) + 1
        try:
            if mode == "read":
                async with lock.read():
                    yield
            else:
                async with lock.write():
                    yield
        finally:
            # Drop the lock object once nobody holds or waits for it, so the
            # table only holds names in use.
            self._lock_users[name] -= 1
            if self._lock_users[name] == 0:
                self._lock_users.pop(name, None)
                self._locks.pop(name, None)

    # ------------------------------------------------------------------- ledger

    def record(self, event: str, **fields: Any) -> None:
        """Append one ledger line.

        Args:
            event: Short event name, for example ``shared_published``.
            **fields: Structured details of that event.
        """
        line = json.dumps({"event": event, "at": time.time(), **fields}, ensure_ascii=False)
        try:
            if self.ledger_path.stat().st_size >= self.ledger_limit_bytes:
                self.ledger_path.replace(self._rotated_ledger)
        except FileNotFoundError:
            pass
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    @property
    def _rotated_ledger(self) -> Path:
        return self.ledger_path.with_name(LEDGER_NAME + ".1")

    def ledger(self) -> list[dict[str, Any]]:
        """Return the retained ledger entries (rotated file first) in write order."""
        rows = []
        for path in (self._rotated_ledger, self.ledger_path):
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        return rows

    # ------------------------------------------------------------ shared storage

    @staticmethod
    def _slug(value: str) -> str:
        return sha256(value.encode()).hexdigest()[:32]

    def _shared_path(self, key: ResourceKey) -> Path:
        return self.shared_directory / (key.slug + ".json")

    def _sweep_shared(self) -> None:
        """Drop expired, unreadable and surplus shared files left by earlier runs."""
        now = time.time()
        for path in self.shared_directory.glob("*.tmp"):
            path.unlink(missing_ok=True)
        alive: list[tuple[float, Path]] = []
        for path in self.shared_directory.glob("*.json"):
            try:
                body = json.loads(path.read_text(encoding="utf-8"))
                published_at = float(body["published_at"])
                expires_at = float(body["expires_at"])
            except (OSError, ValueError, KeyError, TypeError):
                path.unlink(missing_ok=True)
                continue
            if expires_at <= now:
                path.unlink(missing_ok=True)
            else:
                alive.append((published_at, path))
        alive.sort()
        for _, path in alive[: max(0, len(alive) - self.max_entries)]:
            path.unlink(missing_ok=True)

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
        if not hmac.compare_digest(str(body.get("resource", "")), key.value):
            return None
        return PublishedOutcome(
            payload=body["payload"],
            version=body["version"],
            owner=body["owner"],
            published_at=body["published_at"],
            expires_at=body["expires_at"],
            users=len(self._users.get(key.value, set())),
        )
