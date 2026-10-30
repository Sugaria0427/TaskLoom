"""Shared access coordination: keys, permits, single-flight and lock order."""

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from taskloom.contracts.models import ToolRun
from taskloom.execution.coordination import (
    AccessCoordinator,
    AccessPermit,
    FileLease,
    ResourceKey,
    SharingUnavailable,
    check_permit,
    sign_permit,
)
from taskloom.policies.access import SingleFlight

ROOT = "/srv/workspace"


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
    first = ResourceKey.of(request(), workspace_root=ROOT)
    again = ResourceKey.of(
        request(attempt_id="attempt-2", root_task_id="root-2"), workspace_root=ROOT
    )
    other_arguments = ResourceKey.of(request(arguments={"pattern": "a"}), workspace_root=ROOT)
    other_work = ResourceKey.of(request(work_iterations=4), workspace_root=ROOT)
    other_version = ResourceKey.of(request(input_version="v2"), workspace_root=ROOT)

    assert first.value == again.value
    assert first.slug == again.slug
    assert first.value != other_arguments.value
    assert first.value != other_work.value
    assert first.value != other_version.value


def test_permit_round_trip(tmp_path: Path) -> None:
    coordinator = AccessCoordinator(tmp_path / "access")
    key = ResourceKey.of(request(), workspace_root=ROOT)
    permit = coordinator.issue_permit(
        key, holder="attempt-1", owner="attempt-1", mode="write", input_version="v1"
    )

    check_permit(permit, coordinator.secret, holder="attempt-1", input_version="v1")

    assert coordinator.ledger()[0]["event"] == "permit_issued"


def test_permit_rejects_forged_expired_and_misdirected_proofs(tmp_path: Path) -> None:
    coordinator = AccessCoordinator(tmp_path / "access", permit_ttl=1.0)
    key = ResourceKey.of(request(), workspace_root=ROOT)
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
    key = ResourceKey.of(request(), workspace_root=ROOT)
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
    key = ResourceKey.of(request(), workspace_root=ROOT)
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
    key = ResourceKey.of(request(), workspace_root=ROOT)
    coordinator.claim(key, "attempt-1")
    waiter = asyncio.create_task(coordinator.await_outcome(key))
    await asyncio.sleep(0.02)

    await coordinator.fail(key, "tool exited with 1")
    with pytest.raises(RuntimeError, match="tool exited with 1"):
        await waiter

    assert coordinator.claim(key, "attempt-2") is True


async def test_published_outcome_expires(tmp_path: Path) -> None:
    coordinator = AccessCoordinator(tmp_path / "access", share_ttl=0.01)
    key = ResourceKey.of(request(), workspace_root=ROOT)
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
    key = ResourceKey.of(request(), workspace_root=ROOT)
    coordinator.record("shared_published", resource=key.slug, owner="attempt-1", users=2)

    rows = coordinator.ledger()
    assert rows[-1]["owner"] == "attempt-1"
    assert rows[-1]["users"] == 2
    assert json.loads(json.dumps(rows[-1])) == rows[-1]


async def test_await_outcome_after_publish_is_served_immediately(tmp_path: Path) -> None:
    """A waiter admitted before the publish must be served after it, not hang.

    Regression for IKR9K3 review defect 1: ``publish`` used to leave a fresh
    unresolved future behind, so an ``await_outcome`` that lost the scheduling
    race waited on it forever.
    """
    coordinator = AccessCoordinator(tmp_path / "access")
    key = ResourceKey.of(request(), workspace_root=ROOT)
    assert coordinator.claim(key, "attempt-1") is True
    assert coordinator.claim(key, "attempt-2") is False
    published = await coordinator.publish(key, {"files": 1, "words": 3}, "attempt-1")

    outcome = await asyncio.wait_for(coordinator.await_outcome(key), timeout=1.0)

    assert outcome.version == published.version
    assert outcome.payload == {"files": 1, "words": 3}
    assert outcome.owner == "attempt-1"


async def test_await_outcome_after_fail_raises_immediately(tmp_path: Path) -> None:
    """A waiter arriving after the failure gets it at once; the key stays free.

    Regression for IKR9K3 review defect 1: ``fail`` used to leave a fresh
    unresolved future behind, so a late ``await_outcome`` hung instead of
    reporting the failure.
    """
    coordinator = AccessCoordinator(tmp_path / "access")
    key = ResourceKey.of(request(), workspace_root=ROOT)
    assert coordinator.claim(key, "attempt-1") is True
    await coordinator.fail(key, "tool exited with 1")

    with pytest.raises(RuntimeError, match="tool exited with 1"):
        await asyncio.wait_for(coordinator.await_outcome(key), timeout=1.0)

    assert coordinator.claim(key, "attempt-2") is True
    published = await coordinator.publish(key, {"files": 2, "words": 7}, "attempt-2")
    outcome = await asyncio.wait_for(coordinator.await_outcome(key), timeout=1.0)
    assert outcome.version == published.version


def test_resource_key_covers_account_workspace_and_approvals() -> None:
    base = ResourceKey.of(request(), workspace_root=ROOT)
    variants = [
        ResourceKey.of(request(tenant_id="bob"), workspace_root=ROOT),
        ResourceKey.of(request(), workspace_root="/srv/other"),
        ResourceKey.of(request(approved_external_path="/etc/hosts"), workspace_root=ROOT),
        ResourceKey.of(request(command_approved=True), workspace_root=ROOT),
        ResourceKey.of(request(approved_command_cwd=ROOT), workspace_root=ROOT),
    ]

    assert all(variant.value != base.value for variant in variants)
    assert len({variant.value for variant in variants}) == len(variants)


def test_only_pure_staged_input_tools_share() -> None:
    for tool in ("file_stats", "grep", "list_files", "top_words"):
        assert ResourceKey.of(request(tool_name=tool), workspace_root=ROOT).shareable
    for tool in ("read_file", "run_command"):
        assert not ResourceKey.of(request(tool_name=tool), workspace_root=ROOT).shareable
    approved = request(approved_external_path="/etc/hosts")
    assert not ResourceKey.of(approved, workspace_root=ROOT).shareable


def test_unshared_tools_hold_no_shared_lock(tmp_path: Path) -> None:
    coordinator = AccessCoordinator(tmp_path / "access")
    shared = ResourceKey.of(request(), workspace_root=ROOT)
    private = ResourceKey.of(request(tool_name="read_file"), workspace_root=ROOT)

    assert coordinator.tool_locks(shared) == [
        (coordinator.lock_file(coordinator.shared_name(shared)), "write")
    ]
    assert coordinator.tool_locks(private) == []


@pytest.mark.parametrize("content, mode", [(b"", 0o600), (b"x" * 40, 0o644), (b"short", 0o600)])
def test_an_invalid_secret_is_replaced(tmp_path: Path, content: bytes, mode: int) -> None:
    directory = tmp_path / "access"
    directory.mkdir()
    secret = directory / "secret"
    secret.write_bytes(content)
    secret.chmod(mode)

    coordinator = AccessCoordinator(directory)

    assert len(coordinator.secret) >= 32
    assert coordinator.secret != content
    assert secret.stat().st_mode & 0o777 == 0o600
    assert secret.read_bytes() == coordinator.secret
    assert coordinator.ledger()[0]["event"] == "secret_replaced"


def test_a_valid_secret_is_kept(tmp_path: Path) -> None:
    first = AccessCoordinator(tmp_path / "access")
    second = AccessCoordinator(tmp_path / "access")

    assert first.secret == second.secret
    assert len(second.secret) == 32


def test_the_ledger_rotates_and_stays_bounded(tmp_path: Path) -> None:
    coordinator = AccessCoordinator(tmp_path / "access", ledger_limit_bytes=2048)
    for index in range(400):
        coordinator.record("lock_wait", index=index, padding="x" * 40)

    files = list((tmp_path / "access").glob("ledger.jsonl*"))
    assert sorted(path.name for path in files) == ["ledger.jsonl", "ledger.jsonl.1"]
    assert all(path.stat().st_size < 2048 + 200 for path in files)
    rows = coordinator.ledger()
    indexes = [row["index"] for row in rows]
    assert indexes == sorted(indexes)
    assert indexes[-1] == 399
    assert len(rows) < 400


async def test_published_and_failed_tables_are_bounded(tmp_path: Path) -> None:
    coordinator = AccessCoordinator(tmp_path / "access", max_entries=2, share_ttl=0.2)
    keys = [
        ResourceKey.of(request(arguments={"pattern": str(i)}), workspace_root=ROOT)
        for i in range(4)
    ]
    for index, key in enumerate(keys[:3]):
        coordinator.claim(key, f"attempt-{index}")
        await coordinator.publish(key, {"files": index}, f"attempt-{index}")

    assert len(coordinator._published) == 2
    assert await coordinator.lookup(keys[0]) is None
    assert len(list((tmp_path / "access" / "shared").glob("*.json"))) == 2
    assert keys[0].value not in coordinator._users

    coordinator.claim(keys[3], "attempt-3")
    await coordinator.fail(keys[3], "boom")
    assert keys[3].value in coordinator._failed
    await asyncio.sleep(0.25)
    await coordinator.fail(keys[0], "later")

    assert keys[3].value not in coordinator._failed
    assert coordinator._published == {}
    assert list((tmp_path / "access" / "shared").glob("*.json")) == []


async def test_expired_shared_files_are_swept_on_open(tmp_path: Path) -> None:
    first = AccessCoordinator(tmp_path / "access", share_ttl=0.05)
    key = ResourceKey.of(request(), workspace_root=ROOT)
    await first.publish(key, {"files": 1}, "attempt-1")
    assert len(list((tmp_path / "access" / "shared").glob("*.json"))) == 1
    await asyncio.sleep(0.1)

    AccessCoordinator(tmp_path / "access")

    assert list((tmp_path / "access" / "shared").glob("*.json")) == []


async def test_resource_locks_are_reclaimed_after_use(tmp_path: Path) -> None:
    coordinator = AccessCoordinator(tmp_path / "access")
    for index in range(10):
        async with coordinator.resources([f"shared:{index}"], "write"):
            assert f"shared:{index}" in coordinator._locks
    async with coordinator.resources(["shared:a", "shared:b"], "read"):
        pass

    assert coordinator._locks == {}
    assert coordinator._lock_users == {}


async def test_an_exhausted_single_flight_asks_for_an_unshared_run(tmp_path: Path) -> None:
    coordinator = AccessCoordinator(tmp_path / "access")
    coordinator._flight = SingleFlight(max_inflight=1)
    first = ResourceKey.of(request(arguments={"pattern": "a"}), workspace_root=ROOT)
    second = ResourceKey.of(request(arguments={"pattern": "b"}), workspace_root=ROOT)
    coordinator.claim(first, "attempt-1")
    coordinator.claim(second, "attempt-2")
    waiting = asyncio.create_task(coordinator.await_outcome(first))
    await asyncio.sleep(0.02)

    with pytest.raises(SharingUnavailable):
        await coordinator.await_outcome(second)

    await coordinator.publish(first, {"files": 1}, "attempt-1")
    assert (await waiting).payload == {"files": 1}
