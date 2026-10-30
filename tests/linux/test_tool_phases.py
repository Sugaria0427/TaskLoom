import asyncio
import json
import os

import pytest

from taskloom.adapters.executor import ProcessExecutor
from taskloom.adapters.linux import CgroupDomain
from taskloom.contracts.models import ToolRun, digest
from taskloom.domain.model import Outcome

pytestmark = pytest.mark.linux


async def test_staged_worker_memory_limit_expands_only_on_applied_authorization(tmp_path):
    root = os.environ.get("TASKLOOM_CGROUP_ROOT")
    if not root:
        pytest.skip("Requires a delegated writable cgroup v2 domain")
    from pathlib import Path

    domain = CgroupDomain(Path(root))
    workspace = tmp_path / "tasks/root"
    (workspace / "inputs").mkdir(parents=True)
    documents = {"a.txt": "a a b"}
    (workspace / "inputs/a.txt").write_text(documents["a.txt"])
    (workspace / "input.json").write_text(json.dumps({"input_version": digest(documents)}))
    executor = ProcessExecutor(tmp_path, domain)
    await executor.start()
    try:
        state = await executor.run(
            ToolRun(
                attempt_id="staged",
                root_task_id="root",
                tenant_id="alice",
                generation=0,
                tool_name="staged_top_words",
                resource_protocol="phase-v1",
                maximum_memory_bytes=384 * 1048576,
                input_version=digest(documents),
                arguments={"top_n": 2},
            )
        )
        group = Path(state.cgroup_path)
        assert int((group / "memory.max").read_text()) == 128 * 1048576
        for version in (1, 2):
            async with asyncio.timeout(20):
                while True:
                    state = await executor.get("staged")
                    if state.pending_phase and state.pending_phase.version == version:
                        break
                    await asyncio.sleep(0.01)
            assert int((group / "memory.max").read_text()) == (128 + (version - 1) * 128) * 1048576
            await executor.grant_phase(state.pending_phase)
            assert int((group / "memory.max").read_text()) == (128 + version * 128) * 1048576
        async with asyncio.timeout(20):
            while True:
                state = await executor.get("staged")
                if state.quiescent:
                    break
                await asyncio.sleep(0.01)
        assert state.outcome == Outcome.SUCCEEDED
        assert state.result["words"] == 3
        assert state.peak_memory_bytes > 0
        assert not await asyncio.to_thread(group.exists)
    finally:
        await executor.close()
