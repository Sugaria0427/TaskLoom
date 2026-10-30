import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from taskloom.adapters.sqlite import SQLiteRepository
from taskloom.adapters.transport import ExecutorHTTP
from taskloom.bootstrap.apps import executor_app
from taskloom.client.dispatcher import QuarantinedExecution
from taskloom.client.ports import GatewayPort
from taskloom.client.service import ClientService
from taskloom.contracts.models import (
    AttemptState,
    Checkpoint,
    ClientConfig,
    TaskCreate,
    TaskState,
    ToolCall,
    ToolPhase,
    ToolRun,
    digest,
)
from taskloom.domain.model import DomainError, Outcome, Resources, WorkState

MIB = 1048576


def transport(app):
    result = ExecutorHTTP("/unused")
    result.client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://executor"
    )
    return result


@asynccontextmanager
async def client(tmp_path, platform_feedback, config=None):
    config = config or ClientConfig(memory_budget_mib=512, staged_tools=True)
    app = executor_app(tmp_path, config)
    async with app.router.lifespan_context(app):
        profile = tmp_path / "clients/alice"
        repository = SQLiteRepository(profile / "ledger.db")
        gateway = AsyncMock(spec=GatewayPort)
        gateway.feedback.return_value = platform_feedback
        executor = transport(app)
        service = ClientService(repository, gateway, executor, profile, config)
        await service.start()
        try:
            yield service, executor, app
        finally:
            await service.close()
            await repository.close()


async def seed(service, key="root"):
    documents = {"a.txt": "one two two three", "b.txt": "three three"}
    spec = TaskCreate(documents=documents)
    call = ToolCall(call_id="staged", name="staged_top_words", arguments={"top_n": 2})
    task = TaskState(
        task_id=key,
        tenant_id="alice",
        agent_id=key + "-agent",
        specification=spec,
        input_version=digest(documents),
        outcome=Outcome.UNKNOWN,
        checkpoint=Checkpoint(model_round=1, pending_tools=[call]),
        deadline_at=(datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
        step_attempts={key + "-model-0": key + "-model-attempt"},
    )
    workspace = service.directory / "tasks" / key
    (workspace / "inputs").mkdir(parents=True)
    for name, content in documents.items():
        (workspace / "inputs" / name).write_text(content)
    (workspace / "input.json").write_text(
        __import__("json").dumps({"input_version": task.input_version, "tenant_id": "alice"})
    )
    await service._save_task(task)
    await service._save_attempt(
        AttemptState(
            attempt_id=key + "-model-attempt",
            root_task_id=key,
            agent_id=task.agent_id,
            step_id=key + "-model-0",
            tenant_id="alice",
            kind="model",
            generation=0,
            outcome=Outcome.SUCCEEDED,
            settled=True,
            state=WorkState.FINISHED,
        )
    )
    return call


async def test_two_real_staged_workers_use_http_grants_and_release_the_same_ledger(
    tmp_path,
    platform_feedback,
):
    async with client(tmp_path, platform_feedback) as (service, executor, _app):
        calls = [await seed(service, key) for key in ("a", "b")]
        async with asyncio.timeout(15):
            results = await asyncio.gather(
                *(service.tool(key, call, 0) for key, call in zip(("a", "b"), calls, strict=True))
            )
        for result in results:
            assert result["words"] == 6
            assert result["rows"] == [{"word": "three", "count": 3}, {"word": "two", "count": 2}]
            assert result["resource_phases"] == 3
        attempts = [a for a in service.attempts.values() if a.kind == "tool"]
        for attempt in attempts:
            receipt = await executor.query(attempt.attempt_id)
            assert receipt.pid is not None and receipt.quiescent
            assert [grant.version for grant in receipt.phase_grants] == [1, 2]
            assert attempt.phase_allocation.memory_bytes == 384 * MIB
            saved = await service.store.get("attempt", attempt.attempt_id)
            assert saved["phase_version"] == 2 and saved["settled"]
        assert service.tools.pool.available == service.tools.pool.capacity
        assert not service.tools.pool.maximum
        events = await service.store.events("alice", 0)
        assert {event.get("resource_phase", 0) for event in events} >= {0, 1, 2}


async def test_lost_grant_reply_restarts_from_saved_expanded_claim_without_relaunch(
    tmp_path,
    platform_feedback,
):
    async with client(tmp_path, platform_feedback) as (service, executor, app):
        call = await seed(service)
        grant = executor.grant_phase

        async def lose_reply(phase):
            await grant(phase)
            raise httpx.ReadError("reply lost after the executor applied the phase")

        executor.grant_phase = lose_reply
        with pytest.raises(QuarantinedExecution):
            await asyncio.wait_for(service.tool("root", call, 0), 10)
        attempt = next(a for a in service.attempts.values() if a.kind == "tool")
        assert attempt.phase_version == 1
        assert service.tools.pool.allocations[attempt.attempt_id].memory_bytes == 256 * MIB
        receipt = await executor.query(attempt.attempt_id)
        original_pid = receipt.pid
        await service.close()

        repository = SQLiteRepository(service.directory / "ledger.db")
        recovered_executor = transport(app)
        recovered = ClientService(
            repository, service.gateway, recovered_executor, service.directory, service.config
        )
        await recovered.start()
        try:
            assert recovered.tools.pool.allocations[attempt.attempt_id].memory_bytes == 256 * MIB
            resumed = recovered.get("root")
            resumed.cancel_generation += 1
            await recovered._save_task(resumed)
            result = await asyncio.wait_for(recovered.tool("root", call, 0), 10)
            assert result["words"] == 6
            assert (await recovered_executor.query(attempt.attempt_id)).pid == original_pid
            assert not recovered.tools.pool.allocations
        finally:
            await recovered.close()
            await repository.close()


async def test_executor_grant_replay_is_idempotent_and_cancel_rejects_late_grant(
    tmp_path,
    platform_feedback,
):
    async with client(tmp_path, platform_feedback) as (service, executor, _app):
        await seed(service)
        request = ToolRun(
            attempt_id="direct",
            root_task_id="root",
            tenant_id="alice",
            generation=0,
            input_version=service.get("root").input_version,
            tool_name="staged_top_words",
            resource_protocol="phase-v1",
            maximum_memory_bytes=384 * MIB,
            arguments={"top_n": 2},
        )
        await executor.run(request)
        async with asyncio.timeout(10):
            while True:
                state = await executor.query("direct")
                if state.pending_phase:
                    break
                await asyncio.sleep(0.01)
        first = state.pending_phase
        with pytest.raises(DomainError):
            await executor.grant_phase(first.model_copy(update={"generation": 1}))
        receipt = await executor.grant_phase(first)
        grant_file = service.directory / "tasks/root/attempts/direct/phase-grant.json"
        grant_file.unlink()  # crash window: applied receipt exists, wakeup file was not published
        receipt = await executor.grant_phase(first)
        assert len(receipt.phase_grants) == 1 and grant_file.exists()
        assert await executor.cancel("direct", 1)
        with pytest.raises(DomainError):
            await executor.grant_phase(
                ToolPhase(attempt_id="direct", generation=0, version=2, memory_bytes=384 * MIB)
            )
        assert (await executor.query("direct")).quiescent


async def test_queued_expiry_removes_protection_and_does_not_launch_tool(
    tmp_path, platform_feedback
):
    async with client(
        tmp_path, platform_feedback, ClientConfig(protection_seconds=0.01, staged_tools=True)
    ) as (
        service,
        executor,
        _app,
    ):
        call = await seed(service)
        task = service.get("root")
        task.deadline_at = (datetime.now(UTC) + timedelta(seconds=0.05)).isoformat()
        await service._save_task(task)
        service.tools.pool.restore(
            "unknown",
            Resources(slots=2, memory_bytes=512 * MIB),
            Resources(slots=2, memory_bytes=512 * MIB),
        )
        with pytest.raises(DomainError, match="deadline"):
            await service.tool("root", call, 0)
        assert service.tools.snapshot()["protected_attempt_id"] is None
        attempt = next(a for a in service.attempts.values() if a.kind == "tool")
        assert await executor.query(attempt.attempt_id) is None
        assert attempt.settled and attempt.error == "DEADLINE_EXPIRED"
        assert list(service.tools.pool.allocations) == ["unknown"]


async def test_impossible_maximum_has_a_durable_failure_without_launch(tmp_path, platform_feedback):
    async with client(
        tmp_path, platform_feedback, ClientConfig(memory_budget_mib=256, staged_tools=True)
    ) as (service, executor, _app):
        call = await seed(service)
        with pytest.raises(DomainError) as error:
            await service.tool("root", call, 0)
        assert error.value.code == "RESOURCE_TOO_LARGE"
        attempt = next(a for a in service.attempts.values() if a.kind == "tool")
        assert attempt.settled and attempt.outcome == Outcome.FAILED
        assert await executor.query(attempt.attempt_id) is None
        assert not service.tools.pool.maximum


async def test_old_executor_is_rejected_before_any_staged_launch():
    executor = ExecutorHTTP("/unused")
    await executor.client.aclose()
    calls = []

    def old_server(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json={"ready": True, "protection": "process-group"})

    executor.client = httpx.AsyncClient(
        transport=httpx.MockTransport(old_server), base_url="http://executor"
    )
    try:
        with pytest.raises(DomainError) as error:
            await executor.run(
                ToolRun(
                    attempt_id="a",
                    root_task_id="r",
                    tenant_id="alice",
                    generation=0,
                    input_version="v",
                    tool_name="staged_top_words",
                    resource_protocol="phase-v1",
                    maximum_memory_bytes=384 * MIB,
                    arguments={"top_n": 2},
                )
            )
        assert error.value.code == "RESOURCE_PROTOCOL_UNSUPPORTED"
        assert calls == [("GET", "/health/ready")]
    finally:
        await executor.close()


async def test_duplicate_queued_call_does_not_terminalize_the_original_waiter(
    tmp_path, platform_feedback
):
    async with client(tmp_path, platform_feedback) as (service, _executor, _app):
        call = await seed(service)
        held = Resources(slots=2, memory_bytes=512 * MIB)
        service.tools.pool.restore("held", held, held)
        first = asyncio.create_task(service.tool("root", call, 0))
        try:
            async with asyncio.timeout(2):
                while True:
                    if len(service.tools) == 1:
                        break
                    await asyncio.sleep(0.001)
            with pytest.raises(DomainError) as error:
                await service.tool("root", call, 0)
            assert error.value.code == "RESERVATION_CONFLICT"
            attempt = next(a for a in service.attempts.values() if a.kind == "tool")
            assert not attempt.settled and attempt.outcome == Outcome.PENDING
            service.tools.pool.release("held")
            service.tools.wake.set()
            assert (await asyncio.wait_for(first, 10))["words"] == 6
        finally:
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)


async def test_lost_grant_reply_in_session_is_resent_once_without_double_debit(
    tmp_path,
    platform_feedback,
):
    async with client(tmp_path, platform_feedback) as (service, executor, _app):
        call = await seed(service)
        grant = executor.grant_phase
        sent = []

        async def lose_first_reply(phase):
            sent.append(phase.version)
            applied = await grant(phase)
            if len(sent) == 1:
                raise httpx.ReadError("reply lost after the executor applied the phase")
            return applied

        executor.grant_phase = lose_first_reply
        result = await asyncio.wait_for(service.tool("root", call, 0), 15)
        assert result["words"] == 6 and result["resource_phases"] == 3
        assert sent == [1, 1, 2]
        attempt = next(a for a in service.attempts.values() if a.kind == "tool")
        receipt = await executor.query(attempt.attempt_id)
        assert [phase.version for phase in receipt.phase_grants] == [1, 2]
        assert attempt.phase_version == 2 and attempt.settled
        granted = [
            entry["target"]["memory_bytes"]
            for entry in service.tools.pool.history
            if entry["identity"] == attempt.attempt_id and entry["granted"]
        ]
        assert granted == [128 * MIB, 256 * MIB, 384 * MIB]
        assert service.tools.pool.available == service.tools.pool.capacity
        assert not service.tools.expansions
