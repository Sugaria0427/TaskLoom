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


class Dispatcher:
    def __init__(self, capacity: Resources, max_pending: int = 64) -> None:
        self.pool = ResourcePool(capacity)
        self.max_pending = max_pending
        self.queue: FairQueue[Pending[Any]] = FairQueue()
        self.active: dict[str, asyncio.Task[None]] = {}
        self.quarantined: set[str] = set()
        self.wake = asyncio.Event()
        self.loop: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.loop = asyncio.create_task(self._loop())

    async def submit(
        self, identity: str, owner: str, demand: Resources, callback: Callable[[], Awaitable[T]]
    ) -> T:
        if not demand.fits(self.pool.capacity):
            raise ValueError("Demand exceeds total resource capacity")
        if len(self.queue) >= self.max_pending:
            raise ValueError("Local pending queue full")
        future: asyncio.Future[T] = asyncio.get_running_loop().create_future()
        pending = Pending(identity, owner, demand, callback, future)
        self.queue.push(owner, pending)
        self.wake.set()
        try:
            return await future
        except asyncio.CancelledError:
            self.queue.remove(lambda item: item.identity == identity)
            operation = self.active.get(identity)
            if operation:
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
            raise

    async def _loop(self) -> None:
        while True:
            item = self.queue.pop(
                lambda candidate: (
                    candidate.future.cancelled() or candidate.demand.fits(self.pool.available)
                )
            )
            if item is not None:
                if item.future.cancelled():
                    continue
                self.pool.reserve(item.identity, item.demand)
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
        for item in self.queue.remove(lambda _: True):
            item.future.cancel()
        operations = list(self.active.values())
        for task in operations:
            task.cancel()
        await asyncio.gather(*operations, return_exceptions=True)
        if self.loop:
            await asyncio.gather(self.loop, return_exceptions=True)
