from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from taskloom.adapters.sqlite import SQLiteRepository
from taskloom.client.ports import ExecutorPort, GatewayPort
from taskloom.client.service import ClientService
from taskloom.contracts.models import (
    AttemptState,
    ClientConfig,
    TaskCreate,
    TaskState,
    ToolCall,
    ToolClaim,
    ToolRun,
    ToolState,
    digest,
)
from taskloom.domain.model import Outcome, Resources, WorkState
from taskloom.domain.ports import StoredRecord


@pytest.mark.parametrize("legacy", [False, True])
async def test_restart_retains_exact_claim_at_minimum_budget(
    tmp_path, platform_feedback, legacy
) -> None:
    repository = SQLiteRepository(tmp_path / "ledger.db")
    specification = TaskCreate(documents={"a.txt": "one two"})
    request = ToolRun(
        attempt_id="attempt",
        root_task_id="root",
        tenant_id="alice",
        generation=0,
        input_version=digest(specification.documents),
    )
    attempt = AttemptState(
        attempt_id="attempt",
        root_task_id="root",
        tenant_id="alice",
        agent_id="agent",
        step_id="root-model-0-tool-call",
        kind="tool",
        generation=0,
        quiescent=False,
        state=WorkState.RECONCILING,
        outcome=Outcome.UNKNOWN,
        request_payload=None if legacy else request.model_dump(mode="json"),
        tool_claim=None
        if legacy
        else ToolClaim(
            slots=1,
            memory_bytes=128 * 1048576,
            artifact_bytes=3 * 1048576,
        ),
    )
    task = TaskState(
        task_id="root",
        tenant_id="alice",
        agent_id="agent",
        specification=specification,
        input_version=request.input_version,
        outcome=Outcome.UNKNOWN,
        quiescent=False,
        deadline_at=(datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
        step_attempts={attempt.step_id: attempt.attempt_id},
    )
    await repository.apply(
        [
            StoredRecord("task", task.task_id, "alice", task.model_dump(mode="json")),
            StoredRecord("attempt", attempt.attempt_id, "alice", attempt.model_dump(mode="json")),
        ]
    )
    executor = AsyncMock(spec=ExecutorPort)
    executor.query.return_value = ToolState(request=request, outcome=Outcome.PENDING)
    gateway = AsyncMock(spec=GatewayPort)
    gateway.feedback.return_value = platform_feedback
    service = ClientService(
        repository,
        gateway,
        executor,
        tmp_path,
        ClientConfig(
            tool_slots=1,
            memory_budget_mib=128,
        ),
    )
    await service.start()
    try:
        expected = Resources(slots=1, memory_bytes=128 * 1048576, artifact_bytes=3 * 1048576)
        assert service.tools.pool.allocations["attempt"] == expected
        saved = await repository.get("attempt", "attempt")
        assert saved["request_payload"] == request.model_dump(mode="json")
        assert saved["tool_claim"]["memory_bytes"] == expected.memory_bytes
        executor.query.return_value = ToolState(
            request=request,
            outcome=Outcome.SUCCEEDED,
            quiescent=True,
            result={"files": 1, "words": 2},
        )
        result = await service.tool(
            "root", ToolCall(call_id="call", name="file_stats"), model_round=0
        )
        assert result["words"] == 2
        executor.run.assert_not_awaited()
        assert service.tools.pool.available == service.tools.pool.capacity
    finally:
        await service.close()
        await repository.close()
