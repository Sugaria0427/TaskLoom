import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from functools import partial
from time import monotonic
from typing import Any, TypeVar

from taskloom.domain.model import DomainError, Resources
from taskloom.policies.fair import FairQueue
from taskloom.policies.resources import PhaseResourcePool

T = TypeVar("T")

# Lower rank is more urgent. Protection only defers same-or-lower urgency work.
PRIORITY_RANK = {"high": 0, "normal": 1, "low": 2}


class QuarantinedExecution(Exception):
    """The physical resource is still unconfirmed and its reservation must remain."""


@dataclass
class Pending[T]:
    identity: str
    owner: str
    demand: Resources
    callback: Callable[[], Awaitable[T]]
    future: asyncio.Future[T]
    priority: str
    enqueued_at: float
    bypasses: int = 0
    # Only registered large requests may become protection targets.
    large: bool = False


class Dispatcher:
    def __init__(
        self,
        capacity: Resources,
        max_pending: int = 64,
        *,
        check_safe: bool = True,
        protection_seconds: float = 5.0,
        bypass_limit: int = 8,
        protect_large: bool = True,
    ) -> None:
        if protection_seconds <= 0 or bypass_limit < 1:
            raise ValueError("Protection thresholds must be positive")
        self.pool = PhaseResourcePool(capacity, check_safe)
        self.protection_seconds = protection_seconds
        self.bypass_limit = bypass_limit
        self.protect_large = protect_large
        self.protected: Pending[Any] | None = None
        # Absolute targets that running holders asked for but could not yet obtain.
        self.expansions: dict[str, Resources] = {}
        self.max_pending = max_pending
        self.queues: dict[str, FairQueue[Pending[Any]]] = {
            name: FairQueue() for name in ("high", "normal", "low")
        }
        self._schedule = ("high", "high", "normal", "high", "high", "normal", "low")
        self._turn = 0
        self.active: dict[str, asyncio.Task[None]] = {}
        self.quarantined: set[str] = set()
        self.wake = asyncio.Event()
        self.loop: asyncio.Task[None] | None = None

    def _waiting(self) -> list[Pending[Any]]:
        return [item for queue in self.queues.values() for item in queue.items()]

    def _protection_target(self) -> Pending[Any] | None:
        """Pure view of the current target; callers decide whether to commit it."""
        waiting = self._waiting()
        current = self.protected
        if (
            current is not None
            and not current.future.done()
            and any(item is current for item in waiting)
        ):
            return current
        if not self.protect_large:
            return None
        now = monotonic()
        aged = [
            item
            for item in waiting
            if item.large
            and not item.future.done()
            and item.identity not in self.pool.allocations
            and (
                now - item.enqueued_at >= self.protection_seconds
                or item.bypasses >= self.bypass_limit
            )
        ]
        return min(aged, key=lambda item: item.enqueued_at) if aged else None

    def _refresh_protection(self) -> None:
        self.protected = self._protection_target()

    def _expansion_headroom(self) -> Resources:
        total = Resources()
        for identity, target in self.expansions.items():
            current = self.pool.allocations.get(identity)
            if current is not None and current.fits(target):
                total = total + (target - current)
        return total

    def expand(self, identity: str, target: Resources) -> bool:
        """Grant a holder's absolute target, or keep headroom for it against new work."""
        granted = self.pool.grant(identity, target)
        if granted or identity not in self.pool.allocations:
            self.expansions.pop(identity, None)
        else:
            self.expansions[identity] = target
        return granted

    def _eligible(self, item: Pending[Any]) -> bool:
        if item.future.cancelled():
            return True
        if item.identity in self.active:
            return False
        if item.identity in self.pool.allocations:
            # Existing holders must be able to finish and return their resources.
            return True
        allowed, reason = self.pool.decision(item.identity, item.demand)
        if not allowed:
            self.pool.record_blocked(
                item.identity, item.demand, reason or "resource admission denied"
            )
            return False
        headroom = self._expansion_headroom()
        available = self.pool.available
        if any(
            want and want + reserved > free
            for want, reserved, free in zip(
                (item.demand.slots, item.demand.memory_bytes, item.demand.artifact_bytes),
                (headroom.slots, headroom.memory_bytes, headroom.artifact_bytes),
                (available.slots, available.memory_bytes, available.artifact_bytes),
                strict=True,
            )
        ):
            # Holders already own part of their claim; new work must not starve them.
            self.pool.blocked[item.identity] = "capacity reserved for holder expansion"
            return False
        protected = self.protected
        if (
            protected is not None
            and protected is not item
            and PRIORITY_RANK[item.priority] >= PRIORITY_RANK[protected.priority]
        ):
            # Drain conflicting dimensions. Preserve WRR among eligible work;
            # never reserve partial physical allocations for the waiting target.
            if any(
                a and b
                for a, b in zip(
                    (item.demand.slots, item.demand.memory_bytes, item.demand.artifact_bytes),
                    (
                        protected.demand.slots,
                        protected.demand.memory_bytes,
                        protected.demand.artifact_bytes,
                    ),
                    strict=True,
                )
            ):
                self.pool.blocked[item.identity] = "capacity protected for " + protected.identity
                return False
        self.pool.blocked.pop(item.identity, None)
        return True

    def snapshot(self) -> dict[str, object]:
        protected = self._protection_target()
        now = monotonic()
        return {
            "enabled": self.protect_large,
            "protected_attempt_id": protected.identity if protected else None,
            "expansions_waiting": {key: asdict(value) for key, value in self.expansions.items()},
            "waiting": [
                {
                    "attempt_id": item.identity,
                    "root_task_id": item.owner,
                    "priority": item.priority,
                    "wait_seconds": now - item.enqueued_at,
                    "bypass_count": item.bypasses,
                    "large": item.large,
                    "protected": item is protected,
                    "protection_reason": (
                        "bypass_limit" if item.bypasses >= self.bypass_limit else "wait_threshold"
                    )
                    if item is protected
                    else None,
                    "blocked_by": self.pool.blocked.get(item.identity),
                }
                for item in self._waiting()
            ],
        }

    @property
    def queue(self) -> "Dispatcher":
        """Compatibility view for callers that report pending queue length."""
        return self

    def __len__(self) -> int:
        return sum(len(queue) for queue in self.queues.values())

    def set_priority(self, identity: str, priority: str) -> bool:
        """Move a waiting request; running work keeps its current allocation."""
        if priority not in self.queues:
            raise ValueError("Unknown priority")
        for queue in self.queues.values():
            moved = queue.remove(lambda item: item.identity == identity)
            if moved:
                for item in moved:
                    item.priority = priority
                    self.queues[priority].push(item.owner, item)
                self.wake.set()
                return True
        return False

    async def start(self) -> None:
        self.loop = asyncio.create_task(self._loop())

    async def submit(
        self,
        identity: str,
        owner: str,
        demand: Resources,
        callback: Callable[[], Awaitable[T]],
        priority: str = "normal",
        maximum: Resources | None = None,
        large: bool | None = None,
    ) -> T:
        """Queue work; ``large`` defaults to a registered maximum above the initial demand."""
        if maximum is not None and not demand.fits(maximum):
            raise DomainError("RESOURCE_MAXIMUM_CONFLICT", "Initial demand exceeds maximum")
        if not demand.fits(self.pool.capacity):
            raise ValueError("Demand exceeds total resource capacity")
        if priority not in self.queues:
            raise ValueError("Unknown priority")
        if sum(len(queue) for queue in self.queues.values()) >= self.max_pending:
            raise DomainError("LOCAL_QUEUE_FULL", "Local pending queue full", retryable=True)
        if identity in self.active or any(item.identity == identity for item in self._waiting()):
            raise DomainError("RESERVATION_CONFLICT", "Identity already has a dispatcher waiter")
        if identity in self.pool.allocations and self.pool.allocations[identity] != demand:
            raise DomainError("RESERVATION_CONFLICT", "Identity has a different demand")
        self.pool.declare(identity, maximum or demand)
        future: asyncio.Future[T] = asyncio.get_running_loop().create_future()
        pending = Pending(
            identity,
            owner,
            demand,
            callback,
            future,
            priority,
            monotonic(),
            large=(maximum is not None and maximum != demand) if large is None else large,
        )
        self.queues[priority].push(owner, pending)
        self.wake.set()
        try:
            return await future
        except asyncio.CancelledError:
            for queue in self.queues.values():
                queue.remove(lambda item: item.identity == identity)
            operation = self.active.get(identity)
            if operation:
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
            self.pool.forget(identity)
            self._refresh_protection()
            self.wake.set()
            raise

    async def _loop(self) -> None:
        while True:
            self._refresh_protection()
            item = None
            for offset in range(len(self._schedule)):
                index = (self._turn + offset) % len(self._schedule)
                candidate = self.queues[self._schedule[index]].pop(self._eligible)
                if candidate is not None:
                    item = candidate
                    self._turn = (index + 1) % len(self._schedule)
                    break
            if item is not None:
                if item.future.cancelled():
                    self.pool.forget(item.identity)
                    continue
                try:
                    if not self.pool.reserve(item.identity, item.demand):
                        self.queues[item.priority].push(item.owner, item)
                        continue
                except Exception as error:
                    if not item.future.done():
                        item.future.set_exception(error)
                    self.pool.forget(item.identity)
                    continue
                for waiting in self._waiting():
                    if any(
                        a and b
                        for a, b in zip(
                            (
                                item.demand.slots,
                                item.demand.memory_bytes,
                                item.demand.artifact_bytes,
                            ),
                            (
                                waiting.demand.slots,
                                waiting.demand.memory_bytes,
                                waiting.demand.artifact_bytes,
                            ),
                            strict=True,
                        )
                    ):
                        waiting.bypasses += 1
                self.quarantined.discard(item.identity)
                operation = asyncio.create_task(self._run(item))
                self.active[item.identity] = operation
                operation.add_done_callback(partial(self._done, item))
                continue
            self.wake.clear()
            deadlines = (
                [
                    pending.enqueued_at + self.protection_seconds
                    for pending in self._waiting()
                    if pending.large
                    and pending.identity not in self.pool.allocations
                    and not pending.future.done()
                ]
                if self.protect_large and self.protected is None
                else []
            )
            if deadlines:
                try:
                    await asyncio.wait_for(
                        self.wake.wait(), max(0.001, min(deadlines) - monotonic())
                    )
                except TimeoutError:
                    pass
            else:
                await self.wake.wait()

    def _done(self, item: Pending[Any], operation: asyncio.Task[None]) -> None:
        # A task cancelled before its first tick never reaches _run's finally.
        self.expansions.pop(item.identity, None)
        if self.active.get(item.identity) is operation:
            self.active.pop(item.identity)
            if item.identity not in self.quarantined:
                self.pool.release(item.identity)
        if operation.cancelled() and not item.future.done():
            item.future.cancel()
        self.wake.set()

    async def _run(self, item: Pending[Any]) -> None:
        release = True
        try:
            value = await item.callback()
            if not item.future.done():
                item.future.set_result(value)
        except QuarantinedExecution as error:
            release = False
            self.quarantined.add(item.identity)
            if not item.future.done():
                item.future.set_exception(error)
        except asyncio.CancelledError:
            if not item.future.done():
                item.future.cancel()
        except Exception as error:
            if not item.future.done():
                item.future.set_exception(error)
        finally:
            self.expansions.pop(item.identity, None)
            if release:
                self.pool.release(item.identity)
            self.active.pop(item.identity, None)
            self.wake.set()

    async def close(self) -> None:
        if self.loop:
            self.loop.cancel()
        for queue in self.queues.values():
            for item in queue.remove(lambda _: True):
                item.future.cancel()
                self.pool.forget(item.identity)
        operations = list(self.active.values())
        for task in operations:
            task.cancel()
        await asyncio.gather(*operations, return_exceptions=True)
        if self.loop:
            await asyncio.gather(self.loop, return_exceptions=True)
