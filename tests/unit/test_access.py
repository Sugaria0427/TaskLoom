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


async def test_single_flight_cancelled_waiter_does_not_cancel_shared_work() -> None:
    flight = SingleFlight(max_inflight=1)
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def operation() -> int:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return 42

    first = asyncio.create_task(flight.run("key", operation))
    await started.wait()
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    other = asyncio.create_task(flight.run("key", operation))
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.wait_for(other, 1) == 42
    assert calls == 1
    await flight.close()


async def test_single_flight_owns_abandoned_work_until_close() -> None:
    flight = SingleFlight(max_inflight=1)
    started, stopped = asyncio.Event(), asyncio.Event()

    async def operation() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    caller = asyncio.create_task(flight.run("key", operation))
    await started.wait()
    caller.cancel()
    await asyncio.gather(caller, return_exceptions=True)
    with pytest.raises(RuntimeError, match="capacity"):
        await flight.run("other", operation)
    await flight.close()
    assert stopped.is_set()
    with pytest.raises(RuntimeError, match="closed"):
        await flight.run("key", operation)


async def test_multilock_rolls_back_acquired_locks_on_cancel() -> None:
    mutex = KeyedMutex()
    context = mutex.locks(["a", "b"])
    held = mutex.locks(["b"])
    await held.__aenter__()
    entering = asyncio.create_task(context.__aenter__())
    await asyncio.sleep(0)
    entering.cancel()
    await asyncio.gather(entering, return_exceptions=True)
    async with asyncio.timeout(1):
        async with mutex.locks(["a", "a"]):
            pass
    await held.__aexit__(None, None, None)
    async with asyncio.timeout(1):
        async with mutex.locks(["b", "a"]):
            pass
