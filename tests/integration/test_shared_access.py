"""Two real tasks may share one read-only preparation without sharing a process."""

import asyncio
import json
from pathlib import Path

import pytest

from taskloom.adapters.executor import ProcessExecutor
from taskloom.adapters.linux import CgroupDomain
from taskloom.contracts.models import JsonObject, ToolRun, ToolState, digest
from taskloom.domain.model import Outcome
from taskloom.execution.coordination import AccessCoordinator, PublishedOutcome, ResourceKey

DOCUMENTS = {"notes.txt": "one two three", "log.txt": "four five"}
WORK = 200_000


def stage(profile: Path, task_id: str, documents: dict[str, str]) -> str:
    workspace = profile / "tasks" / task_id
    (workspace / "inputs").mkdir(parents=True, exist_ok=True)
    for name, text in documents.items():
        (workspace / "inputs" / name).write_text(text, encoding="utf-8", newline="")
    version = digest(documents)
    (workspace / "input.json").write_text(
        json.dumps({"input_version": version, "tenant_id": "alice"}), encoding="utf-8"
    )
    return version


def request(attempt_id: str, root_task_id: str, version: str, work: int = WORK) -> ToolRun:
    return ToolRun(
        attempt_id=attempt_id,
        root_task_id=root_task_id,
        tenant_id="alice",
        tool_name="file_stats",
        input_version=version,
        work_iterations=work,
        arguments={},
        generation=0,
    )


async def finished(executor: ProcessExecutor, attempt_id: str) -> ToolState:
    async with asyncio.timeout(30):
        while True:
            state = await executor.get(attempt_id)
            assert state is not None
            if state.quiescent:
                return state
            await asyncio.sleep(0.02)


async def test_two_tasks_share_one_initialization(tmp_path: Path) -> None:
    profile = tmp_path / "alice"
    version = stage(profile, "task-a", DOCUMENTS)
    stage(profile, "task-b", DOCUMENTS)
    executor = ProcessExecutor(profile, CgroupDomain(None))
    await executor.start()
    try:
        owner = await executor.run(request("a1", "task-a", version))
        waiter = await executor.run(request("b1", "task-b", version))

        assert owner.pid is not None
        assert waiter.pid is None
        assert not waiter.quiescent

        first = await finished(executor, "a1")
        second = await finished(executor, "b1")

        assert first.outcome == Outcome.SUCCEEDED
        assert second.outcome == Outcome.SUCCEEDED
        assert first.result is not None and second.result is not None
        assert first.result["access"]["role"] == "owner"
        assert second.result["access"]["role"] == "waiter"
        assert first.result["access"]["version"] == second.result["access"]["version"]
        assert first.result["files"] == second.result["files"] == 2
        assert first.result["words"] == second.result["words"] == 5
        assert second.result["artifact_id"] == "b1"
        assert second.result["access"]["owner"] == "a1"
        assert second.result["access"]["users"] == 2
        assert second.result["access"]["waited_ms"] > 0

        shared = [row for row in executor.access.ledger() if row["event"] == "shared_published"]
        assert len(shared) == 1
        assert shared[0]["owner"] == "a1"
        assert shared[0]["users"] == 2
        permits = [row for row in executor.access.ledger() if row["event"] == "permit_issued"]
        assert len(permits) == 1
    finally:
        await executor.close()


async def test_a_later_attempt_reuses_the_published_preparation(tmp_path: Path) -> None:
    profile = tmp_path / "alice"
    version = stage(profile, "task-a", DOCUMENTS)
    stage(profile, "task-c", DOCUMENTS)
    executor = ProcessExecutor(profile, CgroupDomain(None))
    await executor.start()
    try:
        await executor.run(request("a1", "task-a", version))
        first = await finished(executor, "a1")

        later = await executor.run(request("c1", "task-c", version))

        assert later.pid is None
        assert later.quiescent
        assert later.outcome == Outcome.SUCCEEDED
        assert later.result is not None and first.result is not None
        assert later.result["access"]["role"] == "shared"
        assert later.result["access"]["version"] == first.result["access"]["version"]
        assert later.result["words"] == first.result["words"]
    finally:
        await executor.close()


async def test_cancelling_a_waiter_leaves_the_owner_running(tmp_path: Path) -> None:
    profile = tmp_path / "alice"
    version = stage(profile, "task-a", DOCUMENTS)
    stage(profile, "task-b", DOCUMENTS)
    executor = ProcessExecutor(profile, CgroupDomain(None))
    await executor.start()
    try:
        await executor.run(request("a1", "task-a", version, work=2_000_000))
        waiter = await executor.run(request("b1", "task-b", version, work=2_000_000))
        assert waiter.pid is None

        assert await executor.cancel("b1", 1) is True

        cancelled = await finished(executor, "b1")
        owner = await finished(executor, "a1")

        assert cancelled.outcome == Outcome.CANCELLED
        assert owner.outcome == Outcome.SUCCEEDED
        assert owner.result is not None
        assert owner.result["access"]["role"] == "owner"
    finally:
        await executor.close()


async def test_a_changed_workspace_is_not_served_by_the_shared_result(tmp_path: Path) -> None:
    profile = tmp_path / "alice"
    version = stage(profile, "task-a", DOCUMENTS)
    stage(profile, "task-b", DOCUMENTS)
    executor = ProcessExecutor(profile, CgroupDomain(None))
    await executor.start()
    try:
        await executor.run(request("a1", "task-a", version, work=2_000_000))
        waiter = await executor.run(request("b1", "task-b", version, work=2_000_000))
        assert waiter.pid is None

        (profile / "tasks" / "task-b" / "inputs" / "notes.txt").write_text(
            "tampered", encoding="utf-8", newline=""
        )

        await finished(executor, "a1")
        changed = await finished(executor, "b1")

        assert changed.outcome == Outcome.FAILED
        assert changed.error is not None and "INPUT_VERSION_MISMATCH" in changed.error
        assert changed.result is None
    finally:
        await executor.close()


class SlowPublish(AccessCoordinator):
    """Widen the real publish window (write lock + atomic write) for observers."""

    async def publish(self, key: ResourceKey, payload: JsonObject, owner: str) -> PublishedOutcome:
        await asyncio.sleep(0.3)
        return await super().publish(key, payload, owner)


async def test_settling_never_shows_quiescent_pending_to_a_poller(tmp_path: Path) -> None:
    """The watcher must settle every terminal field before quiescent flips.

    Regression for IKR9K3 review defect 2: quiescent used to be set before the
    awaited publish, so a service-style poller (service.py:872) saw
    quiescent=True with outcome=PENDING and reported TOOL_PENDING for a
    preparation that actually succeeded.
    """
    profile = tmp_path / "alice"
    version = stage(profile, "task-a", DOCUMENTS)
    executor = ProcessExecutor(profile, CgroupDomain(None), access=SlowPublish(profile / "access"))
    await executor.start()
    try:
        state = await executor.run(request("a1", "task-a", version, work=50_000))
        dirty = False
        async with asyncio.timeout(30):
            while not state.quiescent and state.outcome == Outcome.PENDING:
                await asyncio.sleep(0.05)
                found = await executor.get("a1")
                assert found is not None
                state = found
                if state.quiescent and state.outcome == Outcome.PENDING:
                    dirty = True

        assert not dirty
        assert state.outcome == Outcome.SUCCEEDED
        assert state.result is not None
        assert state.result["access"]["role"] == "owner"
    finally:
        await executor.close()


async def test_launch_failure_frees_the_claim_for_the_next_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launch that explodes after the claim must fail the key, not leak it.

    Regression for IKR9K3 review defect 3: the claim used to stay in flight
    forever when _launch raised outside its Popen error branch, so every later
    attempt for the same key became a waiter and hung.
    """
    profile = tmp_path / "alice"
    version = stage(profile, "task-a", DOCUMENTS)
    executor = ProcessExecutor(profile, CgroupDomain(None))
    await executor.start()
    try:
        original = executor.cgroups.create

        def broken_create(
            identity: str, memory: int, cpu_quota: int = 100000, cpu_period: int = 100000
        ) -> Path | None:
            raise RuntimeError("cgroup create exploded")

        monkeypatch.setattr(executor.cgroups, "create", broken_create)
        with pytest.raises(RuntimeError, match="cgroup create exploded"):
            await executor.run(request("a1", "task-a", version))
        monkeypatch.setattr(executor.cgroups, "create", original)

        failures = [row for row in executor.access.ledger() if row["event"] == "shared_failed"]
        assert len(failures) == 1
        assert failures[0]["error"] == "RuntimeError: cgroup create exploded"

        retry = await executor.run(request("a2", "task-a", version))
        assert retry.pid is not None  # owner path, not a hanging waiter
        final = await finished(executor, "a2")
        assert final.outcome == Outcome.SUCCEEDED
        assert final.result is not None
        assert final.result["access"]["role"] == "owner"
    finally:
        await executor.close()
