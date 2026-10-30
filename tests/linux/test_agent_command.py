import asyncio
import json
import os
from pathlib import Path

import pytest

from taskloom.adapters.executor import ProcessExecutor
from taskloom.adapters.linux import CgroupDomain, live_group
from taskloom.contracts.models import ToolRun, digest
from taskloom.domain.model import Outcome

pytestmark = pytest.mark.linux


@pytest.mark.parametrize("cancel", [False, True])
async def test_command_descendants_stop_and_cgroup_is_reclaimed(tmp_path, cancel):
    group_root = os.environ.get("TASKLOOM_CGROUP_ROOT")
    if not group_root:
        pytest.skip("Run with tools/project.py test linux")
    root = tmp_path / "project"
    root.mkdir()
    workspace = tmp_path / "tasks" / "root"
    (workspace / "inputs").mkdir(parents=True)
    (workspace / "input.json").write_text(
        json.dumps({"input_version": digest({}), "tenant_id": "alice"})
    )
    executor = ProcessExecutor(tmp_path, CgroupDomain(Path(group_root)), workspace_root=str(root))
    await executor.start()
    request = ToolRun(
        attempt_id="commandcancel" if cancel else "commandtimeout",
        root_task_id="root",
        tenant_id="alice",
        generation=0,
        input_version=digest({}),
        tool_name="run_command",
        arguments={"command": "sleep 30 & wait", "cwd": ".", "timeout_seconds": 1},
        timeout_seconds=30 if cancel else 1,
        workspace_root=str(root),
        command_approved=True,
        approved_command_cwd=str(root),
        memory_bytes=256 * 1048576,
    )
    try:
        state = await executor.run(request)
        assert state.cgroup_path
        if cancel:
            await asyncio.sleep(0.2)
            assert await executor.cancel(request.attempt_id, 1)
        for _ in range(200):
            state = await executor.get(request.attempt_id)
            if state.quiescent:
                break
            await asyncio.sleep(0.05)
        assert state.quiescent
        assert state.outcome == (Outcome.CANCELLED if cancel else Outcome.FAILED)
        if not cancel:
            assert state.error == "TOOL_TIMEOUT"
        assert not await asyncio.to_thread(Path(state.cgroup_path).exists)
        assert not live_group(state.pid, state.start_ticks, state.boot_id)
    finally:
        await executor.close()
