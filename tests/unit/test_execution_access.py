"""Shared access coordination: keys, permits, single-flight and lock order."""

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from taskloom.contracts.models import ToolRun
from taskloom.execution.access import (
    AccessCoordinator,
    AccessPermit,
    FileLease,
    ResourceKey,
    check_permit,
    sign_permit,
)


def request(**overrides: object) -> ToolRun:
    body = {
        "attempt_id": "attempt-1",
        "root_task_id": "root-1",
        "tenant_id": "alice",
        "generation": 0,
        "tool_name": "file_stats",
        "input_version": "v1",
        "work_iterations": 3,
        "arguments": {"pattern": "the"},
    }
    body.update(overrides)
    return ToolRun(**body)


def test_resource_key_is_stable_and_discriminating() -> None:
    first = ResourceKey.of(request())
    again = ResourceKey.of(request(attempt_id="attempt-2", root_task_id="root-2"))
    other_arguments = ResourceKey.of(request(arguments={"pattern": "a"}))
    other_work = ResourceKey.of(request(work_iterations=4))
    other_version = ResourceKey.of(request(input_version="v2"))

    assert first.value == again.value
    assert first.slug == again.slug
    assert first.value != other_arguments.value
    assert first.value != other_work.value
    assert first.value != other_version.value


def test_permit_round_trip(tmp_path: Path) -> None:
    coordinator = AccessCoordinator(tmp_path / "access")
    key = ResourceKey.of(request())
    permit = coordinator.issue_permit(
        key, holder="attempt-1", owner="attempt-1", mode="write", input_version="v1"
    )

    check_permit(permit, coordinator.secret, holder="attempt-1", input_version="v1")

    assert coordinator.ledger()[0]["event"] == "permit_issued"


def test_permit_rejects_forged_expired_and_misdirected_proofs(tmp_path: Path) -> None:
    coordinator = AccessCoordinator(tmp_path / "access", permit_ttl=1.0)
    key = ResourceKey.of(request())
    permit = coordinator.issue_permit(
        key, holder="attempt-1", owner="attempt-1", mode="write", input_version="v1"
    )
    forged = AccessPermit(**{**permit.body(), "signature": "0" * 64})

    with pytest.raises(ValueError, match="signature does not match"):
        check_permit(forged, coordinator.secret)
    with pytest.raises(ValueError, match="has expired"):
        check_permit(permit, coordinator.secret, now=permit.expires_at + 1)
    with pytest.raises(ValueError, match="belongs to another attempt"):
        check_permit(permit, coordinator.secret, holder="attempt-2")
    with pytest.raises(ValueError, match="input version differs"):
        check_permit(permit, coordinator.secret, input_version="v2")


def test_signing_is_keyed_by_the_profile_secret(tmp_path: Path) -> None:
    body = {"permit_id": "p", "holder": "a"}
    assert sign_permit(b"one", body) != sign_permit(b"two", body)
    assert sign_permit(b"one", body) == sign_permit(b"one", body)


async def test_single_flight_runs_the_initialization_once(tmp_path: Path) -> None:
    coordinator = AccessCoordinator(tmp_path / "access")
    key = ResourceKey.of(request())
    runs: list[str] = []

    async def prepare() -> None:
        runs.append("owner")
        await asyncio.sleep(0.05)
        await coordinator.publish(key, {"files": 1, "words": 3}, "attempt-1")

    assert coordinator.claim(key, "attempt-1") is True
    assert coordinator.claim(key, "attempt-2") is False
    owner, waiter = await asyncio.gather(
        prepare(), coordinator.await_outcome(key), return_exceptions=False
    )

    assert owner is None
    assert waiter.payload == {"files": 1, "words": 3}
    assert runs == ["owner"]
    assert coordinator.users(key) == 2


async def test_cancelling_one_waiter_leaves_the_others_served(tmp_path: Path) -> None:
    coordinator = AccessCoordinator(tmp_path / "access")
    key = ResourceKey.of(request())
    coordinator.claim(key, "attempt-1")
    survivor = asyncio.create_task(coordinator.await_outcome(key))
    doomed = asyncio.create_task(coordinator.await_outcome(key))
    await asyncio.sleep(0.02)

    doomed.cancel()
    with pytest.raises(asyncio.CancelledError):
        await doomed

    outcome = await coordinator.publish(key, {"files": 2, "words": 9}, "attempt-1")
    assert (await survivor).payload == outcome.payload


async def test_failure_lets_a_waiter_apply_again(tmp_path: Path) -> None:
    coordinator = AccessCoordinator(tmp_path / "access")
    key = ResourceKey.of(request())
    coordinator.claim(key, "attempt-1")
    waiter = asyncio.create_task(coordinator.await_outcome(key))
    await asyncio.sleep(0.02)

    await coordinator.fail(key, "tool exited with 1")
    with pytest.raises(RuntimeError, match="tool exited with 1"):
        await waiter

    assert coordinator.claim(key, "attempt-2") is True


async def test_published_outcome_expires(tmp_path: Path) -> None:
    coordinator = AccessCoordinator(tmp_path / "access", share_ttl=0.01)
    key = ResourceKey.of(request())
    await coordinator.publish(key, {"files": 1}, "attempt-1")

    assert await coordinator.lookup(key) is not None
    await asyncio.sleep(0.05)
    assert await coordinator.lookup(key) is None


async def test_readers_run_in_parallel_and_a_writer_runs_alone(tmp_path: Path) -> None:
    coordinator = AccessCoordinator(tmp_path / "access")
    inside: list[str] = []
    release = asyncio.Event()

    async def reader(name: str) -> None:
        async with coordinator.resources(["shared:a"], "read"):
            inside.append(name)
            await release.wait()

    first = asyncio.create_task(reader("one"))
    second = asyncio.create_task(reader("two"))
    await asyncio.sleep(0.02)
    assert sorted(inside) == ["one", "two"]

    entered = asyncio.Event()

    async def writer() -> None:
        async with coordinator.resources(["shared:a"], "write"):
            entered.set()

    blocked = asyncio.create_task(writer())
    await asyncio.sleep(0.02)
    assert not entered.is_set()

    release.set()
    await asyncio.gather(first, second, blocked)
    assert entered.is_set()


async def test_reverse_resource_requests_do_not_deadlock(tmp_path: Path) -> None:
    coordinator = AccessCoordinator(tmp_path / "access")

    async def hold(order: list[str]) -> None:
        async with coordinator.resources(order, "write"):
            await asyncio.sleep(0.01)

    await asyncio.wait_for(
        asyncio.gather(hold(["shared:a", "shared:b"]), hold(["shared:b", "shared:a"])),
        timeout=2,
    )
    waits = [row for row in coordinator.ledger() if row["event"] == "lock_wait"]
    assert len(waits) == 2
    assert all(row["keys"] == ["shared:a", "shared:b"] for row in waits)


def test_readers_share_a_file_lease(tmp_path: Path) -> None:
    lock = tmp_path / "shared.lock"
    first = FileLease([(lock, "read")]).__enter__()
    second = FileLease([(lock, "read")]).__enter__()

    assert first.waited_ms >= 0
    second.__exit__(None, None, None)
    first.__exit__(None, None, None)


def test_a_writer_file_lease_waits_for_readers(tmp_path: Path) -> None:
    lock = tmp_path / "shared.lock"
    reader = FileLease([(lock, "read")]).__enter__()
    acquired = threading.Event()

    def write() -> None:
        with FileLease([(lock, "write")]):
            acquired.set()

    worker = threading.Thread(target=write)
    worker.start()
    assert not acquired.wait(0.2)

    reader.__exit__(None, None, None)
    assert acquired.wait(2.0)
    worker.join()


def test_file_leases_are_taken_in_one_order(tmp_path: Path) -> None:
    first_lock = tmp_path / "a.lock"
    second_lock = tmp_path / "b.lock"
    finished: list[int] = []

    def work(reverse: bool) -> None:
        locks: list[tuple[Path, str]] = [(first_lock, "write"), (second_lock, "write")]
        if reverse:
            locks.reverse()
        with FileLease(locks):  # type: ignore[arg-type]
            time.sleep(0.01)
        finished.append(1)

    workers = [threading.Thread(target=work, args=(flag,)) for flag in (False, True)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)

    assert finished == [1, 1]


def test_ledger_records_who_carried_the_shared_preparation(tmp_path: Path) -> None:
    coordinator = AccessCoordinator(tmp_path / "access")
    key = ResourceKey.of(request())
    coordinator.record("shared_published", resource=key.slug, owner="attempt-1", users=2)

    rows = coordinator.ledger()
    assert rows[-1]["owner"] == "attempt-1"
    assert rows[-1]["users"] == 2
    assert json.loads(json.dumps(rows[-1])) == rows[-1]
