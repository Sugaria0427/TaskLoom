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


class SingleFlight:
    """Run one shared operation per key; concurrent callers await the same result."""

    def __init__(self) -> None:
        self._inflight: dict[Hashable, asyncio.Task[Any]] = {}

    async def run(self, key: Hashable, operation: Callable[[], Coroutine[Any, Any, T]]) -> T:
        task = self._inflight.get(key)
        if task is None or task.done():
            task = asyncio.create_task(operation())
            self._inflight[key] = task
        try:
            return await task  # type: ignore[no-any-return]
        finally:
            if self._inflight.get(key) is task:
                self._inflight.pop(key, None)


class MultiLock:
    """Holds several asyncio locks acquired in a single canonical order."""

    def __init__(self, locks: list[asyncio.Lock]) -> None:
        self._locks = locks

    async def __aenter__(self) -> "MultiLock":
        for lock in self._locks:
            await lock.acquire()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        for lock in reversed(self._locks):
            lock.release()
        return False


class KeyedMutex:
    """Named locks; ``locks`` sorts keys so every task acquires them identically."""

    def __init__(self) -> None:
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def locks(self, keys: Iterable[str]) -> MultiLock:
        return MultiLock([self._locks[key] for key in sorted(keys)])


class ReadWriteLock:
    """Readers-writer lock built on ``asyncio.Condition``."""

    def __init__(self) -> None:
        self._readers = 0
        self._writing = False
        self._condition = asyncio.Condition()

    @asynccontextmanager
    async def read(self) -> AsyncIterator[None]:
        async with self._condition:
            await self._condition.wait_for(lambda: not self._writing)
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
            await self._condition.wait_for(lambda: not self._writing and self._readers == 0)
            self._writing = True
        try:
            yield
        finally:
            async with self._condition:
                self._writing = False
                self._condition.notify_all()
