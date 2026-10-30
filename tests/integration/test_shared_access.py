"""Two real tasks may share one read-only preparation without sharing a process."""

import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from taskloom.adapters.executor import ProcessExecutor
from taskloom.adapters.linux import CgroupDomain, live_group
from taskloom.contracts.models import JsonObject, ToolRun, ToolState, digest
from taskloom.domain.model import Outcome
from taskloom.execution.coordination import AccessCoordinator, PublishedOutcome, ResourceKey
from taskloom.policies.access import SingleFlight

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


def tool(attempt_id: str, version: str, tool_name: str, **fields: Any) -> ToolRun:
    return ToolRun(
        attempt_id=attempt_id,
        root_task_id="task-a",
        tenant_id="alice",
        tool_name=tool_name,
        input_version=version,
        generation=0,
        **fields,
    )


async def test_an_unapproved_external_read_is_never_served_from_cache(tmp_path: Path) -> None:
    profile = tmp_path / "alice"
    version = stage(profile, "task-a", DOCUMENTS)
    outside = tmp_path / "outside" / "secret.txt"
    outside.parent.mkdir()
    outside.write_text("top secret", encoding="utf-8")
    arguments = {"path": str(outside), "max_bytes": 1024}
    executor = ProcessExecutor(profile, CgroupDomain(None))
    await executor.start()
    try:
        await executor.run(
            tool(
                "r1",
                version,
                "read_file",
                arguments=arguments,
                approved_external_path=str(outside.resolve()),
            )
        )
        approved = await finished(executor, "r1")
        assert approved.outcome == Outcome.SUCCEEDED
        assert approved.result is not None and approved.result["content"] == "top secret"

        second = await executor.run(tool("r2", version, "read_file", arguments=arguments))
        assert second.pid is not None
        denied = await finished(executor, "r2")

        assert denied.outcome == Outcome.FAILED
        assert denied.result is None
        assert "not approved" in (denied.error or "")
    finally:
        await executor.close()


async def test_a_rewritten_workspace_file_is_read_again(tmp_path: Path) -> None:
    profile = tmp_path / "alice"
    version = stage(profile, "task-a", DOCUMENTS)
    executor = ProcessExecutor(profile, CgroupDomain(None))
    target = executor.workspace_root / "notes.md"
    target.write_text("first", encoding="utf-8")
    arguments = {"path": "notes.md", "max_bytes": 1024}
    await executor.start()
    try:
        await executor.run(tool("r1", version, "read_file", arguments=arguments))
        assert (await finished(executor, "r1")).result["content"] == "first"  # type: ignore[index]

        target.write_text("second", encoding="utf-8")
        await executor.run(tool("r2", version, "read_file", arguments=arguments))
        fresh = await finished(executor, "r2")

        assert fresh.result is not None and fresh.result["content"] == "second"
        assert "access" not in fresh.result
    finally:
        await executor.close()


async def test_the_same_approved_append_runs_every_time(tmp_path: Path) -> None:
    profile = tmp_path / "alice"
    version = stage(profile, "task-a", DOCUMENTS)
    executor = ProcessExecutor(profile, CgroupDomain(None))
    root = str(executor.workspace_root)
    fields: dict[str, Any] = {
        "arguments": {"command": "echo hit >> counter.txt", "cwd": root},
        "command_approved": True,
        "approved_command_cwd": root,
    }
    await executor.start()
    try:
        for attempt in ("c1", "c2"):
            started = await executor.run(tool(attempt, version, "run_command", **fields))
            assert started.pid is not None
            assert (await finished(executor, attempt)).outcome == Outcome.SUCCEEDED

        assert (executor.workspace_root / "counter.txt").read_text() == "hit\nhit\n"
    finally:
        await executor.close()


async def test_cancel_during_process_creation_stops_the_late_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: cancel used to report success while Popen was still creating it."""
    profile = tmp_path / "alice"
    version = stage(profile, "task-a", DOCUMENTS)
    executor = ProcessExecutor(profile, CgroupDomain(None))
    original = subprocess.Popen
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()

    def slow_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        loop.call_soon_threadsafe(entered.set)
        time.sleep(0.5)
        return original(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", slow_popen)
    await executor.start()
    try:
        running = asyncio.create_task(executor.run(request("a1", "task-a", version, 5_000_000)))
        await asyncio.wait_for(entered.wait(), timeout=5)

        assert await executor.cancel("a1", 1) is True
        state = await executor.get("a1")
        assert state is not None
        assert state.quiescent and state.outcome == Outcome.CANCELLED
        assert state.pid is not None
        assert not live_group(state.pid, state.start_ticks, state.boot_id)
        launched = await running
        assert launched.pid == state.pid
        assert not (profile / "tasks" / "task-a" / "attempts" / "a1" / "result.json").exists()
    finally:
        await executor.close()


async def test_cancel_before_admission_starts_nothing(tmp_path: Path) -> None:
    profile = tmp_path / "alice"
    version = stage(profile, "task-a", DOCUMENTS)
    executor = ProcessExecutor(profile, CgroupDomain(None))
    await executor.start()
    try:
        running = asyncio.create_task(executor.run(request("a1", "task-a", version)))
        assert await executor.cancel("a1", 1) is True
        state = await running

        assert state.pid is None
        assert state.quiescent and state.outcome == Outcome.CANCELLED
    finally:
        await executor.close()


async def test_a_waiter_survives_an_executor_restart_as_a_clean_failure(tmp_path: Path) -> None:
    """Regression: a waiter was never saved, so a restart lost it (client TOOL_UNKNOWN)."""
    profile = tmp_path / "alice"
    version = stage(profile, "task-a", DOCUMENTS)
    stage(profile, "task-b", DOCUMENTS)
    first = ProcessExecutor(profile, CgroupDomain(None))
    await first.start()
    try:
        await first.run(request("a1", "task-a", version, work=5_000_000))
        waiter = await first.run(request("b1", "task-b", version, work=5_000_000))
        assert waiter.pid is None and waiter.share_role == "waiter"
        # The saved state as a crash leaves it, opened by a fresh executor.
        crashed = tmp_path / "crashed"
        shutil.copytree(profile, crashed)
        saved = ToolState.model_validate_json(
            (crashed / "executions" / "b1.json").read_text(encoding="utf-8")
        )
        assert saved.share_role == "waiter" and not saved.quiescent

        restarted = ProcessExecutor(crashed, CgroupDomain(None))
        await restarted.start()
        try:
            recovered = await restarted.get("b1")
            assert recovered is not None
            assert recovered.quiescent
            assert recovered.outcome == Outcome.FAILED
            assert (recovered.error or "").startswith("EXECUTOR_RESTARTED")
        finally:
            await restarted.close()
    finally:
        await first.close()


async def test_a_waiter_takes_over_when_the_owner_is_cancelled(tmp_path: Path) -> None:
    profile = tmp_path / "alice"
    version = stage(profile, "task-a", DOCUMENTS)
    stage(profile, "task-b", DOCUMENTS)
    executor = ProcessExecutor(profile, CgroupDomain(None), slots=1)
    await executor.start()
    try:
        await executor.run(request("a1", "task-a", version, work=2_000_000))
        waiter = await executor.run(request("b1", "task-b", version, work=2_000_000))
        assert waiter.pid is None  # admitted although the only slot is taken

        assert await executor.cancel("a1", 1) is True
        taken = await finished(executor, "b1")

        assert taken.outcome == Outcome.SUCCEEDED
        assert taken.pid is not None
        assert taken.result is not None and taken.result["access"]["role"] == "owner"
        assert (await finished(executor, "a1")).outcome == Outcome.CANCELLED
    finally:
        await executor.close()


async def test_an_exhausted_single_flight_runs_the_waiter_unshared(tmp_path: Path) -> None:
    profile = tmp_path / "alice"
    version = stage(profile, "task-a", DOCUMENTS)
    stage(profile, "task-b", DOCUMENTS)
    executor = ProcessExecutor(profile, CgroupDomain(None), slots=4)
    executor.access._flight = SingleFlight(max_inflight=1)
    await executor.start()
    try:
        await executor.run(request("a1", "task-a", version, work=1_000_000))
        await executor.run(request("b1", "task-b", version, work=1_000_000))
        await executor.run(request("c1", "task-a", version, work=1_000_001))
        await executor.run(request("d1", "task-b", version, work=1_000_001))

        states: list[ToolState] = [
            await finished(executor, name) for name in ("a1", "b1", "c1", "d1")
        ]

        assert all(state.outcome == Outcome.SUCCEEDED for state in states)
        assert states[1].result is not None and states[1].result["access"]["role"] == "waiter"
        assert states[3].pid is not None
        assert states[3].result is not None and "access" not in states[3].result
    finally:
        await executor.close()


async def test_a_failed_read_never_shows_a_settled_outcome_before_quiescent(
    tmp_path: Path,
) -> None:
    """Regression: the stderr diagnostic used to be read after outcome was set."""
    profile = tmp_path / "alice"
    version = stage(profile, "task-a", DOCUMENTS)
    executor = ProcessExecutor(profile, CgroupDomain(None))
    await executor.start()
    try:
        state = await executor.run(
            tool("r1", version, "read_file", arguments={"path": "missing.txt", "max_bytes": 64})
        )
        dirty = False
        async with asyncio.timeout(30):
            while not state.quiescent:
                await asyncio.sleep(0)
                found = await executor.get("r1")
                assert found is not None
                state = found
                if state.outcome != Outcome.PENDING and not state.quiescent:
                    dirty = True

        assert not dirty
        assert state.outcome == Outcome.FAILED
        assert "File not found" in (state.error or "")
    finally:
        await executor.close()
