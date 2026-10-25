import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from taskloom.domain.model import ResourcePool, Resources
from taskloom.policies.fair import FairQueue

T = TypeVar("T")


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


class Dispatcher:
    def __init__(self, capacity: Resources, max_pending: int = 64) -> None:
        self.pool = ResourcePool(capacity)
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
    ) -> T:
        if not demand.fits(self.pool.capacity):
            raise ValueError("Demand exceeds total resource capacity")
        if priority not in self.queues:
            raise ValueError("Unknown priority")
        if sum(len(queue) for queue in self.queues.values()) >= self.max_pending:
            raise ValueError("Local pending queue full")
        future: asyncio.Future[T] = asyncio.get_running_loop().create_future()
        pending = Pending(identity, owner, demand, callback, future, priority)
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
            raise

    async def _loop(self) -> None:
        while True:
            item = None
            for offset in range(len(self._schedule)):
                index = (self._turn + offset) % len(self._schedule)
                candidate = self.queues[self._schedule[index]].pop(
                    lambda pending: (
                        pending.future.cancelled()
                        or (
                            pending.identity not in self.active
                            and (
                                pending.identity in self.pool.allocations
                                or pending.demand.fits(self.pool.available)
                            )
                        )
                    )
                )
                if candidate is not None:
                    item = candidate
                    self._turn = (index + 1) % len(self._schedule)
                    break
            if item is not None:
                if item.future.cancelled():
                    continue
                try:
                    if not self.pool.reserve(item.identity, item.demand):
                        self.queues[item.priority].push(item.owner, item)
                        continue
                except Exception as error:
                    if not item.future.done():
                        item.future.set_exception(error)
                    continue
                self.quarantined.discard(item.identity)
                self.active[item.identity] = asyncio.create_task(self._run(item))
                continue
            self.wake.clear()
            await self.wake.wait()

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
        operations = list(self.active.values())
        for task in operations:
            task.cancel()
        await asyncio.gather(*operations, return_exceptions=True)
        if self.loop:
            await asyncio.gather(self.loop, return_exceptions=True)
