"""Concurrency primitives for shared initialization and file modification.

A shared workspace is prepared once (single-flight), read by many callers and
written by one at a time (read-write lock), and multiple locks are always taken
in one canonical order so two tasks can never deadlock waiting on each other.
"""

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Coroutine, Hashable, Iterable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

T = TypeVar("T")


class CapacityExhausted(RuntimeError):
    """A SingleFlight already tracks its maximum number of distinct keys."""


class SingleFlight:
    """Run one shared operation per key; concurrent callers await the same result."""

    def __init__(self, max_inflight: int = 64) -> None:
        if max_inflight < 1:
            raise ValueError("max_inflight must be positive")
        self._inflight: dict[Hashable, asyncio.Task[Any]] = {}
        self._max_inflight = max_inflight
        self._closed = False

    async def run(self, key: Hashable, operation: Callable[[], Coroutine[Any, Any, T]]) -> T:
        if self._closed:
            raise RuntimeError("SingleFlight is closed")
        task = self._inflight.get(key)
        if task is None or task.done():
            if task is None and len(self._inflight) >= self._max_inflight:
                raise CapacityExhausted("SingleFlight capacity exhausted")
            task = asyncio.create_task(operation())
            self._inflight[key] = task
            task.add_done_callback(lambda done: self._completed(key, done))
        return await asyncio.shield(task)

    def _completed(self, key: Hashable, task: asyncio.Task[Any]) -> None:
        if self._inflight.get(key) is task:
            self._inflight.pop(key, None)
        if not task.cancelled():
            task.exception()  # Retrieve errors even when every caller has left.

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()


class MultiLock:
    """Holds several asyncio locks acquired in a single canonical order."""

    def __init__(self, locks: list[asyncio.Lock]) -> None:
        self._locks = list(dict.fromkeys(locks))
        self._acquired: list[asyncio.Lock] = []

    async def __aenter__(self) -> "MultiLock":
        try:
            for lock in self._locks:
                await lock.acquire()
                self._acquired.append(lock)
        except BaseException:
            self._release()
            raise
        return self

    def _release(self) -> None:
        for lock in reversed(self._acquired):
            lock.release()
        self._acquired.clear()

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        self._release()
        return False


class KeyedMutex:
    """Named locks; ``locks`` sorts keys so every task acquires them identically."""

    def __init__(self) -> None:
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def locks(self, keys: Iterable[str]) -> MultiLock:
        return MultiLock([self._locks[key] for key in sorted(set(keys))])


class ReadWriteLock:
    """Readers-writer lock built on ``asyncio.Condition`` that prefers writers.

    A new reader waits while a writer is waiting, so a steady stream of readers
    cannot starve a writer; readers already inside finish first.
    """

    def __init__(self) -> None:
        self._readers = 0
        self._writing = False
        self._waiting_writers = 0
        self._condition = asyncio.Condition()

    @asynccontextmanager
    async def read(self) -> AsyncIterator[None]:
        async with self._condition:
            await self._condition.wait_for(lambda: not self._writing and self._waiting_writers == 0)
            self._readers += 1
        try:
            yield
        finally:
            async with self._condition:
                self._readers -= 1
                if self._readers == 0:
                    self._condition.notify_all()

    @asynccontextmanager
    async def write(self) -> AsyncIterator[None]:
        async with self._condition:
            self._waiting_writers += 1
            try:
                await self._condition.wait_for(lambda: not self._writing and self._readers == 0)
            except BaseException:
                self._waiting_writers -= 1
                # Readers held back for this writer may proceed now.
                self._condition.notify_all()
                raise
            self._waiting_writers -= 1
            self._writing = True
        try:
            yield
        finally:
            async with self._condition:
                self._writing = False
                self._condition.notify_all()
