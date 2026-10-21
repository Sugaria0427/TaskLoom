import asyncio

import pytest

from taskloom.policies.access import KeyedMutex, ReadWriteLock, SingleFlight


async def test_single_flight_runs_shared_operation_once() -> None:
    flight = SingleFlight()
    calls = 0

    async def operation() -> int:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return 42

    results = await asyncio.gather(flight.run("init", operation), flight.run("init", operation))
    assert results == [42, 42]
    assert calls == 1


async def test_single_flight_retries_after_failure() -> None:
    flight = SingleFlight()
    attempts = 0

    async def flaky() -> int:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("boom")
        return 7

    with pytest.raises(RuntimeError):
        await flight.run("k", flaky)
    assert await flight.run("k", flaky) == 7
    assert attempts == 2


async def test_read_write_lock_allows_concurrent_readers() -> None:
    lock = ReadWriteLock()
    active = 0
    peak = 0

    async def reader() -> None:
        nonlocal active, peak
        async with lock.read():
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.05)
            active -= 1

    await asyncio.gather(reader(), reader(), reader())
    assert peak == 3


async def test_read_write_lock_writer_is_exclusive() -> None:
    lock = ReadWriteLock()
    writing = False
    reader_during_write = False

    async def reader() -> None:
        nonlocal reader_during_write
        async with lock.read():
            if writing:
                reader_during_write = True
            await asyncio.sleep(0.01)

    async def writer() -> None:
        nonlocal writing
        async with lock.write():
            writing = True
            await asyncio.sleep(0.05)
            writing = False

    await asyncio.gather(writer(), reader())
    assert not reader_during_write


async def test_keyed_mutex_orders_overlapping_locks_identically() -> None:
    mutex = KeyedMutex()
    order: list[str] = []

    async def first() -> None:
        async with mutex.locks(["b", "a"]):
            order.append("first")
            await asyncio.sleep(0.02)

    async def second() -> None:
        async with mutex.locks(["a", "b"]):
            order.append("second")

    await asyncio.wait_for(asyncio.gather(first(), second()), timeout=1)
    assert order == ["first", "second"]
