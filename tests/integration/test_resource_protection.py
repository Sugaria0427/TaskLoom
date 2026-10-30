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
                "big",
                "root-big",
                Resources(slots=1, memory_bytes=6),
                large,
                large=True,
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
        dispatcher.submit(
            "big", "big", Resources(slots=1, memory_bytes=4), release.wait, large=True
        )
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
            "big",
            "big",
            Resources(slots=1, memory_bytes=10),
            lambda: asyncio.sleep(0),
            large=True,
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


MIB = 1048576


async def _hold(dispatcher, key, priority, memory, gate, large=None, maximum=None):
    demand = Resources(slots=1, memory_bytes=memory * MIB)
    return asyncio.create_task(
        dispatcher.submit(
            key,
            key,
            demand,
            gate.wait,
            priority=priority,
            maximum=maximum,
            large=large,
        )
    )


async def test_one_shot_tools_never_become_protection_targets():
    # Review scenario: A(low) holds 400 MiB, B(low) waits for 300 MiB past both
    # thresholds, C(high) and D(low) ask for 50 MiB. Ordinary tools register
    # maximum == demand (staged_tools=false), so admission must match main.
    dispatcher = Dispatcher(
        Resources(slots=4, memory_bytes=512 * MIB), protection_seconds=0.02, bypass_limit=1
    )
    await dispatcher.start()
    gate = asyncio.Event()
    jobs = []
    try:
        a = Resources(slots=1, memory_bytes=400 * MIB)
        jobs.append(await _hold(dispatcher, "a", "low", 400, gate, maximum=a))
        await eventually(lambda: "a" in dispatcher.active)
        b = Resources(slots=1, memory_bytes=300 * MIB)
        jobs.append(await _hold(dispatcher, "b", "low", 300, gate, maximum=b))
        await asyncio.sleep(0.06)
        for key, priority in (("c", "high"), ("d", "low")):
            small = Resources(slots=1, memory_bytes=50 * MIB)
            jobs.append(await _hold(dispatcher, key, priority, 50, gate, maximum=small))
        await eventually(lambda: {"c", "d"} <= set(dispatcher.active))
        assert dispatcher.protected is None
        assert dispatcher.snapshot()["protected_attempt_id"] is None
    finally:
        gate.set()
        await asyncio.gather(*jobs, return_exceptions=True)
        await dispatcher.close()


def test_default_registry_has_no_large_tools_without_staged_tools():
    from taskloom.execution.catalog import tool_profile
    from taskloom.runtime.registry import agent_registry, default_registry

    for registry in (default_registry(), agent_registry()):
        for name in registry.names():
            assert tool_profile(name).maximum is None, name
    assert "staged_top_words" in default_registry(staged=True).names()


async def test_protection_never_defers_more_urgent_requests():
    dispatcher = Dispatcher(Resources(slots=4, memory_bytes=512 * MIB), protection_seconds=0.02)
    await dispatcher.start()
    gate = asyncio.Event()
    jobs = []
    try:
        jobs.append(await _hold(dispatcher, "a", "low", 400, gate))
        await eventually(lambda: "a" in dispatcher.active)
        jobs.append(await _hold(dispatcher, "b", "normal", 300, gate, large=True))
        await eventually(lambda: dispatcher.protected is not None)
        for key, priority in (("c", "high"), ("d", "normal"), ("e", "low")):
            jobs.append(await _hold(dispatcher, key, priority, 50, gate))
        await eventually(lambda: "c" in dispatcher.active)
        await asyncio.sleep(0.05)
        assert "d" not in dispatcher.active and "e" not in dispatcher.active
        assert dispatcher.pool.blocked["d"] == "capacity protected for b"
    finally:
        gate.set()
        await asyncio.gather(*jobs, return_exceptions=True)
        await dispatcher.close()


async def test_snapshot_reports_protection_without_changing_scheduler_state():
    dispatcher = Dispatcher(Resources(slots=1, memory_bytes=4), protection_seconds=0.01)
    waiter = asyncio.create_task(
        dispatcher.submit(
            "big", "big", Resources(slots=1, memory_bytes=4), lambda: asyncio.sleep(0), large=True
        )
    )
    try:
        await eventually(lambda: len(dispatcher) == 1)
        await asyncio.sleep(0.02)
        assert dispatcher.snapshot()["protected_attempt_id"] == "big"
        assert dispatcher.protected is None
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        await dispatcher.close()


async def test_holder_expansion_is_not_starved_by_arriving_one_shot_tools():
    dispatcher = Dispatcher(Resources(slots=4, memory_bytes=512 * MIB))
    await dispatcher.start()
    order = []
    expanded = asyncio.Event()
    b_gate, done = asyncio.Event(), asyncio.Event()
    target = Resources(slots=1, memory_bytes=384 * MIB)

    go = asyncio.Event()

    async def holder():
        await go.wait()
        for _ in range(100):  # same cadence as the client poll loop
            if dispatcher.expand("a", target):
                break
            await asyncio.sleep(0.05)
        order.append("a-expanded")
        expanded.set()
        await done.wait()

    async def light(key):
        order.append(key)
        await done.wait()

    jobs = [
        asyncio.create_task(
            dispatcher.submit(
                "a", "a", Resources(slots=1, memory_bytes=256 * MIB), holder, maximum=target
            )
        )
    ]
    try:
        await eventually(lambda: "a" in dispatcher.active)
        jobs.append(
            asyncio.create_task(
                dispatcher.submit("b", "b", Resources(slots=1, memory_bytes=256 * MIB), b_gate.wait)
            )
        )
        await eventually(lambda: "b" in dispatcher.active)
        go.set()
        await eventually(lambda: "a" in dispatcher.expansions)
        for key in ("c", "d"):
            jobs.append(
                asyncio.create_task(
                    dispatcher.submit(
                        key,
                        key,
                        Resources(slots=1, memory_bytes=256 * MIB),
                        lambda key=key: light(key),
                    )
                )
            )
        await eventually(lambda: len(dispatcher) == 2)
        b_gate.set()
        await asyncio.wait_for(expanded.wait(), 2)
        assert order == ["a-expanded"]
        assert dispatcher.pool.blocked["c"] in (
            "capacity reserved for holder expansion",
            "resources unavailable",
        )
        assert not dispatcher.expansions
        done.set()
        await asyncio.wait_for(asyncio.gather(*jobs), 2)
        assert set(order) == {"a-expanded", "c", "d"}
        assert not dispatcher.pool.allocations
    finally:
        done.set()
        b_gate.set()
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        await dispatcher.close()


async def test_new_identity_queues_behind_restored_unsafe_occupancy():
    dispatcher = Dispatcher(Resources(slots=3, memory_bytes=10))
    for key in ("x", "y"):
        dispatcher.pool.restore(
            key, Resources(slots=1, memory_bytes=4), Resources(slots=1, memory_bytes=10)
        )
        dispatcher.quarantined.add(key)
    assert dispatcher.pool.snapshot()["safe_sequence"] is None
    await dispatcher.start()
    fresh = asyncio.create_task(
        dispatcher.submit(
            "fresh",
            "fresh",
            Resources(slots=1, memory_bytes=1),
            lambda: asyncio.sleep(0, "done"),
            maximum=Resources(slots=1, memory_bytes=2),
        )
    )
    try:
        await eventually(lambda: len(dispatcher) == 1)
        await asyncio.sleep(0.02)
        assert not fresh.done()
        assert dispatcher.pool.blocked["fresh"] == "would break the safe sequence"
        dispatcher.pool.release("x")
        dispatcher.quarantined.discard("x")
        dispatcher.wake.set()
        assert await asyncio.wait_for(fresh, 1) == "done"
    finally:
        fresh.cancel()
        await asyncio.gather(fresh, return_exceptions=True)
        await dispatcher.close()
