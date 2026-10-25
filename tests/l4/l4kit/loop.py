"""Virtual-time asyncio loop: timers fire in order, idle waits cost no wall time.

Only in-process work may run on this loop. Threads (asyncio.to_thread, SQLite writer)
complete through real I/O and would let virtual time race ahead of them.
"""

import asyncio
import selectors
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

# Wall-clock origin for the platform clock, so ISO timestamps are reproducible. Loop time
# itself starts at zero: near 1.8e9 a float step (~2e-7 s) exceeds asyncio's clock
# resolution, and a timer due exactly "now" would then spin without ever firing.
EPOCH = 1_800_000_000.0


class VirtualSelector(selectors.BaseSelector):
    def __init__(self, loop: "VirtualEventLoop") -> None:
        self._real = selectors.DefaultSelector()
        self._loop = loop

    def register(self, fileobj: Any, events: int, data: Any = None) -> selectors.SelectorKey:
        return self._real.register(fileobj, events, data)

    def unregister(self, fileobj: Any) -> selectors.SelectorKey:
        return self._real.unregister(fileobj)

    def modify(self, fileobj: Any, events: int, data: Any = None) -> selectors.SelectorKey:
        return self._real.modify(fileobj, events, data)

    def get_map(self) -> Any:
        return self._real.get_map()

    def close(self) -> None:
        self._real.close()

    def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
        ready = self._real.select(0)
        if ready or (timeout is not None and timeout <= 0):
            return ready
        if timeout is None:
            # No timer is pending, so only real I/O can make progress.
            ready = self._real.select(5)
            if not ready:
                raise RuntimeError("Virtual loop is idle with no timers or I/O")
            return ready
        self._loop.advance(timeout)
        return []


class VirtualEventLoop(asyncio.SelectorEventLoop):
    def __init__(self) -> None:
        self.virtual = 0.0
        super().__init__(VirtualSelector(self))

    def time(self) -> float:
        return self.virtual

    def wall(self) -> float:
        return EPOCH + self.virtual

    def advance(self, seconds: float) -> None:
        self.virtual += seconds


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


def run_virtual[T](main: Callable[[], Awaitable[T]]) -> T:
    loop = VirtualEventLoop()
    try:
        return loop.run_until_complete(main())
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
