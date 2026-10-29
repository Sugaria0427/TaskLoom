"""Two real tasks may share one read-only preparation without sharing a process."""

import asyncio
import json
from pathlib import Path

from taskloom.adapters.executor import ProcessExecutor
from taskloom.adapters.linux import CgroupDomain
from taskloom.contracts.models import ToolRun, ToolState, digest
from taskloom.domain.model import Outcome

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
