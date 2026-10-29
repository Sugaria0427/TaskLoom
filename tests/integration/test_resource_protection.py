import asyncio

import pytest

from taskloom.client.dispatcher import Dispatcher
from taskloom.domain.model import Resources


async def eventually(predicate):
    async with asyncio.timeout(2):
        while True:
            if predicate():
                return
            await asyncio.sleep(0.001)


async def test_large_tool_is_admitted_while_competing_producer_remains_live():
    dispatcher = Dispatcher(
        Resources(slots=3, memory_bytes=6), bypass_limit=2, protection_seconds=60
    )
    await dispatcher.start()
    gates = {}
    started = []
    jobs = []

    async def small(key):
        started.append(key)
        gates[key] = asyncio.Event()
        await gates[key].wait()

    def enqueue(key):
        job = asyncio.create_task(
            dispatcher.submit(key, key, Resources(slots=1, memory_bytes=2), lambda: small(key))
        )
        jobs.append(job)

    big_started, big_release = asyncio.Event(), asyncio.Event()

    async def large():
        big_started.set()
        await big_release.wait()

    try:
        for key in ("a", "b", "c"):
            enqueue(key)
        await eventually(lambda: len(started) == 3)
        big = asyncio.create_task(
            dispatcher.submit(
                "big", "root-big", Resources(slots=1, memory_bytes=6), large, priority="low"
            )
        )
        jobs.append(big)
        await eventually(lambda: len(dispatcher) == 1)
        for old, new in (("a", "d"), ("b", "e")):
            enqueue(new)
            await eventually(lambda: len(dispatcher) >= 2)
            gates[old].set()
            await eventually(lambda key=new: key in started)
        await eventually(lambda: dispatcher.snapshot()["protected_attempt_id"] == "big")
        enqueue("next")  # more light work is still arriving
        for gate in list(gates.values()):
            gate.set()
        await asyncio.wait_for(big_started.wait(), 2)
        assert "next" not in started
        assert dispatcher.pool.allocations["big"].memory_bytes == 6
        big_release.set()
        await eventually(lambda: "next" in started)
        gates["next"].set()
        await asyncio.gather(*jobs)
        assert not dispatcher.pool.allocations
        assert not dispatcher.pool.maximum
    finally:
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        await dispatcher.close()


async def test_timer_activates_protection_without_an_external_wakeup_and_cancel_clears_it():
    dispatcher = Dispatcher(Resources(slots=2, memory_bytes=4), protection_seconds=0.02)
    await dispatcher.start()
    release = asyncio.Event()
    running = asyncio.create_task(
        dispatcher.submit("held", "held", Resources(slots=1, memory_bytes=2), release.wait)
    )
    await eventually(lambda: "held" in dispatcher.active)
    big = asyncio.create_task(
        dispatcher.submit("big", "big", Resources(slots=1, memory_bytes=4), release.wait)
    )
    try:
        await eventually(lambda: dispatcher.protected is not None)
        big.cancel()
        await asyncio.gather(big, return_exceptions=True)
        assert dispatcher.snapshot()["protected_attempt_id"] is None
        assert "big" not in dispatcher.pool.maximum
        assert (
            await asyncio.wait_for(
                dispatcher.submit(
                    "small", "small", Resources(slots=1, memory_bytes=2), lambda: asyncio.sleep(0)
                ),
                1,
            )
            is None
        )
    finally:
        release.set()
        await running
        await dispatcher.close()


async def test_protection_does_not_prevent_existing_phase_holder_from_finishing():
    dispatcher = Dispatcher(Resources(slots=2, memory_bytes=10), protection_seconds=0.01)
    dispatcher.pool.restore(
        "held", Resources(slots=1, memory_bytes=4), Resources(slots=1, memory_bytes=10)
    )
    dispatcher.quarantined.add("held")
    await dispatcher.start()
    big = asyncio.create_task(
        dispatcher.submit(
            "big", "big", Resources(slots=1, memory_bytes=10), lambda: asyncio.sleep(0)
        )
    )
    try:
        await eventually(lambda: dispatcher.protected is not None)
        assert dispatcher.pool.grant("held", Resources(slots=1, memory_bytes=10))
        dispatcher.pool.release("held")
        dispatcher.wake.set()
        await asyncio.wait_for(big, 1)
    finally:
        big.cancel()
        await asyncio.gather(big, return_exceptions=True)
        await dispatcher.close()


async def test_cancel_between_reservation_and_first_tick_releases_waiter_and_capacity(monkeypatch):
    dispatcher = Dispatcher(Resources(slots=1))
    original = dispatcher._run
    started = False

    def cancel_on_launch(item):
        coroutine = original(item)
        asyncio.get_running_loop().call_soon(lambda: dispatcher.active[item.identity].cancel())
        return coroutine

    async def callback():
        nonlocal started
        started = True

    monkeypatch.setattr(dispatcher, "_run", cancel_on_launch)
    await dispatcher.start()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(dispatcher.submit("a", "root", Resources(slots=1), callback), 1)
        assert not started
        assert dispatcher.pool.available == dispatcher.pool.capacity
        assert not dispatcher.pool.maximum
    finally:
        await dispatcher.close()
