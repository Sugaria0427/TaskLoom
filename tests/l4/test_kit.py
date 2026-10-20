"""Self-tests of the L4 kit, so scenario results can be trusted."""

import asyncio
import time

from l4kit import (
    MemoryRepository,
    Scenario,
    Workload,
    history,
    mixed_workload,
    run_scenario,
    run_virtual,
    sized_messages,
)

from taskloom.contracts.models import mock_tokens
from taskloom.domain.ports import StoredRecord


def test_virtual_loop_skips_idle_time_and_keeps_timer_order() -> None:
    order: list[str] = []

    async def main() -> float:
        async def wake(name: str, delay: float) -> None:
            await asyncio.sleep(delay)
            order.append(name)

        await asyncio.gather(wake("late", 3600), wake("early", 0.001), wake("middle", 60))
        return asyncio.get_running_loop().time()

    started = time.monotonic()
    assert run_virtual(main) == 3600
    assert time.monotonic() - started < 1
    assert order == ["early", "middle", "late"]


def test_sized_messages_match_the_mock_token_contract() -> None:
    for tokens in (20, 300, 8000):
        measured = mock_tokens({"messages": sized_messages(tokens), "tools": []})
        assert abs(measured - tokens) <= 1


def test_workload_round_trips_through_json() -> None:
    workload = mixed_workload(seed=7, users=3, tasks_per_user=4)
    assert Workload.from_json(workload.to_json()) == workload


def test_memory_repository_isolates_copies_and_deduplicates_events() -> None:
    async def main() -> None:
        store = MemoryRepository()
        data = {"value": [1]}
        event = {"event_id": "e1", "tenant_id": "alice"}
        await store.apply([StoredRecord("k", "1", "alice", data)], event)
        await store.apply([StoredRecord("k", "2", "bob", {"value": [2]})], event)
        data["value"].append(99)
        assert await store.get("k", "1") == {"value": [1]}
        assert len(await store.records("k")) == 2
        assert await store.records("k", "bob") == [{"value": [2]}]
        assert [e["event_id"] for e in await store.events("alice", 0)] == ["e1"]

    run_virtual(main)


def test_same_workload_replays_identically() -> None:
    def fingerprint() -> list[tuple[object, ...]]:
        result = run_scenario(Scenario(mixed_workload(seed=3, users=3, tasks_per_user=4)))
        return [
            (r.tenant_id, r.request.request_id, r.state, r.dispatched_at, r.consumed_units)
            for r in sorted(history(result.service), key=lambda r: r.request.request_id)
        ]

    assert fingerprint() == fingerprint()
