import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from taskloom.adapters.sqlite import SQLiteRepository
from taskloom.client.service import ClientService
from taskloom.contracts.models import (
    AttemptState,
    ClientConfig,
    GatewayRecord,
    ModelRequest,
    PlatformFeedback,
    RootRegistration,
    TaskCreate,
    TaskState,
    ToolCall,
    ToolRun,
    ToolState,
    digest,
    tool_step_id,
    utc_now,
)
from taskloom.domain.model import DomainError, Outcome, WorkState


class UnusedGateway:
    async def feedback(self) -> PlatformFeedback:
        raise AssertionError("gateway is not used by this test")

    async def register(self, root: RootRegistration) -> None:
        raise AssertionError("gateway is not used by this test")

    async def submit(self, request: ModelRequest) -> GatewayRecord:
        raise AssertionError("gateway is not used by this test")

    async def query(self, request_id: str) -> GatewayRecord:
        raise AssertionError("gateway is not used by this test")

    async def cancel(self, request_id: str) -> GatewayRecord:
        raise AssertionError("gateway is not used by this test")

    async def close(self) -> None:
        return None


class RecordingExecutor:
    def __init__(self) -> None:
        self.requests: list[ToolRun] = []
        self.states: dict[str, ToolState] = {}

    async def run(self, request: ToolRun) -> ToolState:
        self.requests.append(request)
        state = ToolState(
            request=request,
            outcome=Outcome.SUCCEEDED,
            quiescent=True,
            result={
                "files": {"a.txt": 3},
                "words": 3,
                "tool_name": request.tool_name,
                "arguments": request.arguments,
            },
        )
        self.states[request.attempt_id] = state
        return state

    async def query(self, attempt_id: str) -> ToolState | None:
        return self.states.get(attempt_id)

    async def cancel(self, attempt_id: str, generation: int) -> bool:
        return True

    async def close(self) -> None:
        return None


def model_attempt(task_id: str, model_round: int) -> AttemptState:
    step_id = f"{task_id}-model-{model_round}"
    return AttemptState(
        attempt_id=step_id + "-attempt",
        root_task_id=task_id,
        agent_id="agent",
        step_id=step_id,
        tenant_id="alice",
        kind="model",
        generation=0,
        state=WorkState.FINISHED,
        outcome=Outcome.SUCCEEDED,
        quiescent=True,
        settled=True,
    )


async def test_tool_identity_is_round_scoped_and_conflicts_are_stable(tmp_path: Path) -> None:
    store = SQLiteRepository(tmp_path / "ledger.db")
    executor = RecordingExecutor()
    profile = tmp_path / "profile"
    service = ClientService(
        store,
        UnusedGateway(),
        executor,
        profile,
        ClientConfig(profile="alice", tenant_id="alice"),
    )
    task_id = "root"
    specification = TaskCreate(documents={"a.txt": "one two three"})
    model_zero = model_attempt(task_id, 0)
    model_one = model_attempt(task_id, 1)
    task = TaskState(
        task_id=task_id,
        tenant_id="alice",
        agent_id="agent",
        specification=specification,
        input_version=digest(specification.documents),
        deadline_at=(datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
        created_at=utc_now(),
        step_attempts={
            model_zero.step_id: model_zero.attempt_id,
            model_one.step_id: model_one.attempt_id,
        },
    )
    service.tasks[task_id] = task
    service.attempts[model_zero.attempt_id] = model_zero
    service.attempts[model_one.attempt_id] = model_one
    await service._save_task(task)
    await service.tools.start()
    call = ToolCall(call_id="c1", name="grep", arguments={"pattern": "two"})
    try:
        first = await service.tool(task_id, call, model_round=0)
        second = await service.tool(task_id, call, model_round=1)
        replay = await service.tool(task_id, call, model_round=0)

        assert len(executor.requests) == 2
        assert first["words"] == second["words"] == replay["words"] == 3
        first_step = f"{task_id}-model-0-tool-c1"
        second_step = f"{task_id}-model-1-tool-c1"
        assert first_step in service.tasks[task_id].step_attempts
        assert second_step in service.tasks[task_id].step_attempts
        assert (
            service.tasks[task_id].step_attempts[first_step]
            != service.tasks[task_id].step_attempts[second_step]
        )

        changed_arguments = ToolCall(call_id="c1", name="grep", arguments={"pattern": "three"})
        with pytest.raises(DomainError) as argument_error:
            await service.tool(task_id, changed_arguments, model_round=0)
        assert argument_error.value.code == "ATTEMPT_CONFLICT"

        changed_tool = ToolCall(call_id="c1", name="file_stats", arguments={})
        with pytest.raises(DomainError) as tool_error:
            await service.tool(task_id, changed_tool, model_round=0)
        assert tool_error.value.code == "ATTEMPT_CONFLICT"
        assert len(executor.requests) == 2
    finally:
        await service.tools.close()
        await store.close()
        await executor.close()


async def test_client_rejects_unknown_tool_before_creating_an_attempt(tmp_path: Path) -> None:
    store = SQLiteRepository(tmp_path / "unknown-ledger.db")
    executor = RecordingExecutor()
    service = ClientService(
        store,
        UnusedGateway(),
        executor,
        tmp_path / "unknown-profile",
        ClientConfig(profile="alice", tenant_id="alice"),
    )
    task_id = "root"
    specification = TaskCreate(documents={"a.txt": "one two three"})
    task = TaskState(
        task_id=task_id,
        tenant_id="alice",
        agent_id="agent",
        specification=specification,
        input_version=digest(specification.documents),
        deadline_at=(datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
        created_at=utc_now(),
    )
    service.tasks[task_id] = task
    await service.tools.start()
    try:
        with pytest.raises(DomainError) as error:
            await service.tool(
                task_id,
                ToolCall(call_id="c1", name="rm_rf", arguments={}),
                model_round=0,
            )

        assert error.value.code == "INVALID_TOOL"
        assert tool_step_id(task_id, 0, "c1") not in service.tasks[task_id].step_attempts
        assert executor.requests == []
    finally:
        await service.tools.close()
        await store.close()
        await executor.close()


async def test_legacy_attempt_without_request_payload_fails_closed(tmp_path: Path) -> None:
    store = SQLiteRepository(tmp_path / "legacy-ledger.db")
    executor = RecordingExecutor()
    service = ClientService(
        store,
        UnusedGateway(),
        executor,
        tmp_path / "legacy-profile",
        ClientConfig(profile="alice", tenant_id="alice"),
    )
    task_id = "root"
    specification = TaskCreate(documents={"a.txt": "one two three"})
    step_id = tool_step_id(task_id, 0, "c1")
    legacy = AttemptState(
        attempt_id="legacy-tool-attempt",
        root_task_id=task_id,
        agent_id="agent",
        step_id=step_id,
        tenant_id="alice",
        kind="tool",
        generation=0,
        state=WorkState.FINISHED,
        outcome=Outcome.SUCCEEDED,
        quiescent=True,
        settled=True,
        result={"files": {"a.txt": 3}, "words": 3},
    )
    task = TaskState(
        task_id=task_id,
        tenant_id="alice",
        agent_id="agent",
        specification=specification,
        input_version=digest(specification.documents),
        deadline_at=(datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
        created_at=utc_now(),
        step_attempts={step_id: legacy.attempt_id},
    )
    service.tasks[task_id] = task
    service.attempts[legacy.attempt_id] = legacy
    await service.tools.start()
    try:
        with pytest.raises(DomainError) as error:
            await service.tool(
                task_id,
                ToolCall(call_id="c1", name="grep", arguments={"pattern": "two"}),
                model_round=0,
            )

        assert error.value.code == "ATTEMPT_CONFLICT"
        assert executor.requests == []
    finally:
        await service.tools.close()
        await store.close()
        await executor.close()


async def test_tool_terminal_settlement_is_idempotent_across_observers(tmp_path: Path) -> None:
    store = SQLiteRepository(tmp_path / "ledger.db")
    executor = RecordingExecutor()
    service = ClientService(store, UnusedGateway(), executor, tmp_path, ClientConfig())
    specification = TaskCreate()
    task = TaskState(
        task_id="root",
        agent_id="agent",
        tenant_id="alice",
        specification=specification,
        input_version=digest(specification.documents),
        deadline_at=(datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
    )
    service.tasks["root"] = task
    request = ToolRun(
        attempt_id="tool",
        root_task_id="root",
        tenant_id="alice",
        generation=0,
        input_version=task.input_version,
    )
    attempt = AttemptState(
        attempt_id="tool",
        root_task_id="root",
        agent_id="agent",
        tenant_id="alice",
        step_id="step",
        kind="tool",
        generation=0,
        quiescent=False,
        request_payload=request.model_dump(mode="json"),
    )
    await service._save_attempt(attempt)
    finished = ToolState(
        request=request, outcome=Outcome.SUCCEEDED, quiescent=True, result={"files": 1, "words": 3}
    )
    try:
        await asyncio.gather(
            service._settle_tool(attempt.model_copy(deep=True), finished),
            service._settle_tool(attempt.model_copy(deep=True), finished),
        )
        stale = await service._settle_tool(
            attempt.model_copy(deep=True), ToolState(request=request)
        )
        assert stale.outcome == Outcome.SUCCEEDED and stale.quiescent
        await service._save_attempt(attempt)
        saved = await store.get("attempt", "tool")
        assert saved["outcome"] == Outcome.SUCCEEDED and saved["settled"]
        events = await store.events("alice", 0)
        assert len([event for event in events if event["type"] == "tool.finished"]) == 1
    finally:
        await store.close()
