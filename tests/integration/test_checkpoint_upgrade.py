import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from taskloom.adapters.sqlite import SQLiteRepository
from taskloom.client.ports import ExecutorPort, GatewayPort
from taskloom.client.service import ClientService
from taskloom.contracts.models import (
    AttemptState,
    Checkpoint,
    ClientConfig,
    GatewayRecord,
    ModelReply,
    TaskCreate,
    TaskState,
    ToolCall,
    ToolRun,
    ToolState,
    Usage,
    digest,
    tool_step_id,
)
from taskloom.domain.model import DomainError, Outcome, Step, WorkState
from taskloom.domain.ports import StoredRecord


def legacy_state(*, unresolved=False, cancelled=False, ambiguous=False):
    specification = TaskCreate(documents={"a.txt": "one two three"})
    call = ToolCall(call_id="stats", name="file_stats", arguments={})
    reply = ModelReply(
        tool_calls=[call], usage=Usage(input_tokens=1, output_tokens=1, cost_units=2)
    )
    result = {"files": 1, "words": 3, "input_version": digest(specification.documents)}
    messages = [
        {"role": "user", "content": "Count all words."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "stats",
                    "type": "function",
                    "function": {"name": "file_stats", "arguments": "{}"},
                }
            ],
        },
    ]
    if not unresolved:
        messages.append({"role": "tool", "tool_call_id": "stats", "content": json.dumps(result)})
    model = AttemptState(
        attempt_id="original-model",
        root_task_id="root",
        agent_id="agent",
        step_id="root-model-0",
        tenant_id="alice",
        kind="model",
        generation=0,
        state=WorkState.FINISHED,
        outcome=Outcome.SUCCEEDED,
        quiescent=True,
        settled=True,
        result=reply.model_dump(mode="json"),
    )
    tool = AttemptState(
        attempt_id="original-tool",
        root_task_id="root",
        agent_id="agent",
        step_id="root-tool-stats",
        tenant_id="alice",
        kind="tool",
        generation=0,
        state=WorkState.RECONCILING if unresolved else WorkState.FINISHED,
        outcome=Outcome.UNKNOWN if unresolved else Outcome.SUCCEEDED,
        quiescent=not unresolved,
        result=None if unresolved else result,
    )
    task = TaskState(
        task_id="root",
        tenant_id="alice",
        agent_id="agent",
        specification=specification,
        input_version=digest(specification.documents),
        checkpoint=Checkpoint(
            messages=messages,
            model_round=1,
            pending_tools=[call] if unresolved else [],
            completed_tools={} if unresolved else {"stats": result},
        ),
        step_attempts={model.step_id: model.attempt_id, tool.step_id: tool.attempt_id},
        deadline_at=(datetime.now(UTC) + timedelta(seconds=120)).isoformat(),
        cancel_requested=cancelled,
        outcome=Outcome.CANCELLED if cancelled else Outcome.PENDING,
        quiescent=not unresolved,
    )
    attempts = [model, tool]
    if ambiguous:
        repeated = model.model_copy(
            update={"attempt_id": "second-model", "step_id": "root-model-1"}
        )
        attempts.append(repeated)
        task.step_attempts[repeated.step_id] = repeated.attempt_id
    return task, attempts


async def seed(store, directory, task, attempts):
    workspace = directory / "tasks" / task.task_id
    (workspace / "inputs").mkdir(parents=True)
    for name, content in task.specification.documents.items():
        (workspace / "inputs" / name).write_text(content, encoding="utf-8")
    original = task.model_dump(mode="json")
    original.pop("tool_step_aliases")
    original["checkpoint"].pop("completed_tool_calls")
    records = [StoredRecord("task", task.task_id, task.tenant_id, original)]
    for attempt in attempts:
        records.extend(
            [
                StoredRecord(
                    "attempt",
                    attempt.attempt_id,
                    attempt.tenant_id,
                    attempt.model_dump(mode="json"),
                ),
                StoredRecord(
                    "step",
                    attempt.step_id,
                    attempt.tenant_id,
                    asdict(
                        Step(
                            step_id=attempt.step_id,
                            root_task_id=task.task_id,
                            agent_id=task.agent_id,
                            kind=attempt.kind,
                            input_version=task.input_version,
                            predecessors=() if attempt.kind == "model" else ("root-model-0",),
                            current_attempt_id=attempt.attempt_id,
                        )
                    ),
                ),
            ]
        )
    await store.apply(records)


def ports(platform_feedback):
    gateway = AsyncMock(spec=GatewayPort)
    gateway.feedback.return_value = platform_feedback

    async def submit(request):
        return GatewayRecord(
            tenant_id="alice",
            request=request,
            payload_hash=digest(request.model_dump(mode="json")),
            state="FINISHED",
            outcome=Outcome.SUCCEEDED,
            quiescent=True,
            consumed_units=2,
            reply=ModelReply(
                content='{"files":1,"words":3,"summary":"verified"}',
                usage=Usage(input_tokens=1, output_tokens=1, cost_units=2),
            ),
        )

    gateway.submit.side_effect = submit
    return gateway, AsyncMock(spec=ExecutorPort)


async def runner_finishes(service):
    runner = service.runners.get("root")
    if runner:
        await asyncio.wait_for(asyncio.shield(runner), 3)


async def test_completed_checkpoint_upgrades_and_keeps_physical_step_identity(
    tmp_path,
    platform_feedback,
):
    store = SQLiteRepository(tmp_path / "client.db")
    task, attempts = legacy_state()
    await seed(store, tmp_path, task, attempts)
    gateway, executor = ports(platform_feedback)
    service = ClientService(store, gateway, executor, tmp_path, ClientConfig())
    await service.start()
    try:
        await runner_finishes(service)
        result = service.get("root")
        assert result.outcome == Outcome.SUCCEEDED and result.quiescent
        canonical = tool_step_id("root", 0, "stats")
        assert result.tool_step_aliases == {canonical: "root-tool-stats"}
        assert result.step_attempts["root-tool-stats"] == "original-tool"
        assert set(result.checkpoint.completed_tools) == {canonical}
        assert result.checkpoint.completed_tool_calls[canonical].name == "file_stats"
        assert (await store.get("step", "root-model-1"))["predecessors"] == ["root-tool-stats"]
        assert len(await store.records("step", "alice")) == 3
        executor.run.assert_not_awaited()
        assert gateway.submit.await_count == 1
        saved = await store.get("checkpoint_backup", "root")
        assert saved["task"]["checkpoint"]["completed_tools"] == task.checkpoint.completed_tools
        assert saved["attempts"][1]["attempt_id"] == "original-tool"
    finally:
        await service.close()
        await store.close()

    # A second actual startup keeps the same alias, history and backup.
    store = SQLiteRepository(tmp_path / "client.db")
    gateway, executor = ports(platform_feedback)
    restarted = ClientService(store, gateway, executor, tmp_path, ClientConfig())
    await restarted.start()
    try:
        assert restarted.get("root").tool_step_aliases == {canonical: "root-tool-stats"}
        assert len(await store.records("checkpoint_backup", "alice")) == 1
        assert (await restarted.resume("root")).outcome == Outcome.SUCCEEDED
        gateway.submit.assert_not_awaited()
    finally:
        await restarted.close()
        await store.close()


async def test_unconfirmed_checkpoint_only_queries_original_attempt_then_resumes(
    tmp_path,
    platform_feedback,
):
    store = SQLiteRepository(tmp_path / "client.db")
    task, attempts = legacy_state(unresolved=True)
    await seed(store, tmp_path, task, attempts)
    gateway, executor = ports(platform_feedback)
    receipt = ToolState(
        request=ToolRun(
            attempt_id="original-tool",
            root_task_id="root",
            tenant_id="alice",
            generation=0,
            input_version=task.input_version,
        ),
        outcome=Outcome.UNKNOWN,
        quiescent=False,
    )
    executor.query.return_value = receipt
    service = ClientService(store, gateway, executor, tmp_path, ClientConfig())
    await service.start()
    try:
        await runner_finishes(service)
        assert service.get("root").outcome == Outcome.UNKNOWN
        assert not service.get("root").quiescent
        executor.run.assert_not_awaited()
        assert set(service.tools.pool.allocations) == {"original-tool"}
        assert all(call.args == ("original-tool",) for call in executor.query.await_args_list)

        receipt = receipt.model_copy(
            update={
                "outcome": Outcome.SUCCEEDED,
                "quiescent": True,
                "result": {"files": 1, "words": 3, "input_version": task.input_version},
            }
        )
        executor.query.return_value = receipt
        await service.reconcile()
        assert not service.tools.pool.allocations
        await service.resume("root")
        await runner_finishes(service)
        assert service.get("root").outcome == Outcome.SUCCEEDED
        executor.run.assert_not_awaited()
        assert service.get("root").step_attempts["root-tool-stats"] == "original-tool"
    finally:
        await service.close()
        await store.close()


async def test_missing_executor_record_never_replays_unknown_work(tmp_path, platform_feedback):
    store = SQLiteRepository(tmp_path / "client.db")
    task, attempts = legacy_state(unresolved=True)
    await seed(store, tmp_path, task, attempts)
    gateway, executor = ports(platform_feedback)
    executor.query.return_value = None
    service = ClientService(store, gateway, executor, tmp_path, ClientConfig())
    await service.start()
    try:
        await runner_finishes(service)
        assert service.get("root").outcome == Outcome.UNKNOWN
        assert set(service.tools.pool.allocations) == {"original-tool"}
        executor.run.assert_not_awaited()
    finally:
        await service.close()
        await store.close()


async def test_mismatched_executor_receipt_keeps_unknown_occupancy(tmp_path, platform_feedback):
    store = SQLiteRepository(tmp_path / "client.db")
    task, attempts = legacy_state(unresolved=True)
    await seed(store, tmp_path, task, attempts)
    gateway, executor = ports(platform_feedback)
    executor.query.return_value = ToolState(
        request=ToolRun(
            attempt_id="original-tool",
            root_task_id="root",
            tenant_id="alice",
            generation=0,
            input_version=task.input_version,
            tool_name="grep",
            arguments={"pattern": "two"},
        ),
        outcome=Outcome.UNKNOWN,
        quiescent=False,
    )
    service = ClientService(store, gateway, executor, tmp_path, ClientConfig())
    await service.start()
    try:
        await runner_finishes(service)
        assert service.get("root").outcome == Outcome.UNKNOWN
        assert not service.attempts["original-tool"].quiescent
        assert set(service.tools.pool.allocations) == {"original-tool"}
        executor.run.assert_not_awaited()
    finally:
        await service.close()
        await store.close()


async def test_cancelled_checkpoint_stays_cancelled_until_explicit_resume(
    tmp_path,
    platform_feedback,
):
    store = SQLiteRepository(tmp_path / "client.db")
    task, attempts = legacy_state(cancelled=True)
    await seed(store, tmp_path, task, attempts)
    gateway, executor = ports(platform_feedback)
    service = ClientService(store, gateway, executor, tmp_path, ClientConfig())
    await service.start()
    try:
        assert service.get("root").cancel_requested
        assert service.get("root").outcome == Outcome.CANCELLED
        assert not service.runners
        await service.resume("root")
        await runner_finishes(service)
        assert service.get("root").outcome == Outcome.SUCCEEDED
        executor.run.assert_not_awaited()
    finally:
        await service.close()
        await store.close()


@pytest.mark.parametrize("ambiguous", [False, True])
async def test_unverifiable_checkpoint_is_held_before_automatic_dispatch(
    tmp_path,
    platform_feedback,
    ambiguous,
):
    store = SQLiteRepository(tmp_path / "client.db")
    task, attempts = legacy_state(ambiguous=ambiguous)
    if not ambiguous:
        attempts[0].result = None
    await seed(store, tmp_path, task, attempts)
    gateway, executor = ports(platform_feedback)
    service = ClientService(store, gateway, executor, tmp_path, ClientConfig())
    await service.start()
    try:
        held = service.get("root")
        assert held.outcome == Outcome.UNKNOWN
        assert held.error.startswith("CHECKPOINT_UPGRADE_REQUIRED")
        assert held.step_attempts == task.step_attempts
        assert held.checkpoint.completed_tools == task.checkpoint.completed_tools
        assert not held.tool_step_aliases and not service.runners
        with pytest.raises(DomainError) as error:
            await service.resume("root")
        assert error.value.code == "CHECKPOINT_UPGRADE_REQUIRED"
        gateway.submit.assert_not_awaited()
        executor.run.assert_not_awaited()
        assert len(await store.records("checkpoint_backup", "alice")) == 1
    finally:
        await service.close()
        await store.close()
