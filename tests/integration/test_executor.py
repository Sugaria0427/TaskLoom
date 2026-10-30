import asyncio
import json

import pytest

from taskloom.adapters.executor import ProcessExecutor
from taskloom.adapters.linux import CgroupDomain, live_group
from taskloom.contracts.models import ToolRun, digest
from taskloom.domain.model import DomainError, Outcome


def staged(tmp_path, task_id: str = "root"):
    profile = tmp_path / "alice"
    workspace = profile / "tasks" / task_id
    (workspace / "inputs").mkdir(parents=True)
    (workspace / "inputs/notes.txt").write_text("one two three")
    version = digest({"notes.txt": "one two three"})
    (workspace / "input.json").write_text(json.dumps({"input_version": version}))
    return profile, version


async def tool_finished(executor, attempt: str):
    async with asyncio.timeout(10):
        while True:
            state = await executor.get(attempt)
            if state.quiescent:
                return state
            await asyncio.sleep(0.02)


async def test_real_tool_drains_large_output_and_publishes_result(tmp_path) -> None:
    profile, version = staged(tmp_path)
    executor = ProcessExecutor(profile, CgroupDomain(None))
    await executor.start()
    try:
        started = await executor.run(
            ToolRun(
                attempt_id="output",
                root_task_id="root",
                tenant_id="alice",
                generation=0,
                input_version=version,
                emit_bytes=3145728,
            )
        )
        assert started.pid is not None
        result = await tool_finished(executor, "output")
        assert result.outcome == Outcome.SUCCEEDED
        assert result.result["words"] == 3
        assert result.stderr_bytes == 3145728
        assert result.output_truncated
        assert (profile / "tasks/root/attempts/output/stderr.log").stat().st_size == 1048576
        assert not live_group(result.pid, result.start_ticks, result.boot_id)
    finally:
        await executor.close()


async def test_real_grep_tool_searches_staged_files(tmp_path) -> None:
    profile, version = staged(tmp_path)
    executor = ProcessExecutor(profile, CgroupDomain(None))
    await executor.start()
    try:
        started = await executor.run(
            ToolRun(
                attempt_id="match",
                root_task_id="root",
                tenant_id="alice",
                generation=0,
                input_version=version,
                tool_name="grep",
                arguments={"pattern": "two"},
            )
        )
        assert started.pid is not None
        result = await tool_finished(executor, "match")
        assert result.outcome == Outcome.SUCCEEDED
        assert result.result["files"] == 1
        assert result.result["matches"] == 1
        assert result.result["words"] == 3
        assert result.result["lines"] == [{"file": "notes.txt", "line": 1, "text": "one two three"}]
    finally:
        await executor.close()


async def test_real_grep_tool_reports_zero_matches(tmp_path) -> None:
    profile, version = staged(tmp_path)
    executor = ProcessExecutor(profile, CgroupDomain(None))
    await executor.start()
    try:
        await executor.run(
            ToolRun(
                attempt_id="miss",
                root_task_id="root",
                tenant_id="alice",
                generation=0,
                input_version=version,
                tool_name="grep",
                arguments={"pattern": "absent"},
            )
        )
        result = await tool_finished(executor, "miss")
        assert result.outcome == Outcome.SUCCEEDED
        assert result.result["matches"] == 0
        assert result.result["words"] == 0
        assert result.result["lines"] == []
    finally:
        await executor.close()


async def test_cancel_running_and_late_launch_tombstone(tmp_path) -> None:
    profile, version = staged(tmp_path)
    executor = ProcessExecutor(profile, CgroupDomain(None))
    await executor.start()
    try:
        await executor.cancel("late", 1)
        rejected = await executor.run(
            ToolRun(
                attempt_id="late",
                root_task_id="root",
                tenant_id="alice",
                generation=0,
                input_version=version,
            )
        )
        assert rejected.outcome == Outcome.CANCELLED and rejected.pid is None
        state = await executor.run(
            ToolRun(
                attempt_id="long",
                root_task_id="root",
                tenant_id="alice",
                generation=0,
                input_version=version,
                work_iterations=5000000,
            )
        )
        assert state.pid
        assert await executor.cancel("long", 1)
        result = await executor.get("long")
        assert result.quiescent and result.outcome == Outcome.CANCELLED
        assert not live_group(state.pid, state.start_ticks, state.boot_id)
    finally:
        await executor.close()


async def test_input_version_checks_actual_file_contents(tmp_path) -> None:
    profile, version = staged(tmp_path)
    (profile / "tasks/root/inputs/notes.txt").write_text("modified after staging")
    executor = ProcessExecutor(profile, CgroupDomain(None))
    await executor.start()
    try:
        with pytest.raises(DomainError, match="contents changed"):
            await executor.run(
                ToolRun(
                    attempt_id="changed",
                    root_task_id="root",
                    tenant_id="alice",
                    generation=0,
                    input_version=version,
                )
            )
        assert not executor.processes
    finally:
        await executor.close()


async def test_unknown_tool_fails_without_publishing_result(tmp_path) -> None:
    profile, version = staged(tmp_path)
    executor = ProcessExecutor(profile, CgroupDomain(None))
    await executor.start()
    try:
        with pytest.raises(ValueError, match="tool_name"):
            await executor.run(
                ToolRun(
                    attempt_id="unknown",
                    root_task_id="root",
                    tenant_id="alice",
                    generation=0,
                    tool_name="rm_rf",
                    input_version=version,
                )
            )
        assert not executor.processes and not executor.states
        assert not (profile / "tasks/root/attempts/unknown/result.json").exists()
    finally:
        await executor.close()


async def test_failed_tool_publishes_error_with_its_terminal_outcome(tmp_path) -> None:
    """Regression for the watcher settlement order: while stderr is read for the
    diagnostic, no observer may see a terminal outcome without its error, or a
    quiescent receipt that is still PENDING."""
    profile, version = staged(tmp_path)
    (profile / "workspace").mkdir()
    executor = ProcessExecutor(profile, CgroupDomain(None))
    await executor.start()
    observed = []
    try:
        await executor.run(
            ToolRun(
                attempt_id="missing",
                root_task_id="root",
                tenant_id="alice",
                generation=0,
                input_version=version,
                tool_name="read_file",
                arguments={"path": "missing.txt", "max_bytes": 1024},
                workspace_root=str(profile / "workspace"),
            )
        )
        async with asyncio.timeout(10):
            while True:
                state = await executor.get("missing")
                observed.append(state)
                if state.quiescent:
                    break
                await asyncio.sleep(0)
        for snapshot in observed:
            if snapshot.outcome != Outcome.PENDING:
                assert snapshot.quiescent and snapshot.error, snapshot
            if snapshot.quiescent:
                assert snapshot.outcome != Outcome.PENDING
        assert observed[-1].outcome == Outcome.FAILED
        assert "File not found" in observed[-1].error
    finally:
        await executor.close()
