"""Use the real L4 feedback with PR #1's real scheduler in the combined snapshot."""

import asyncio
import json
import math
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from taskloom.adapters.sqlite import SQLiteRepository
from taskloom.adapters.transport import PlatformHTTP
from taskloom.bootstrap.apps import platform_app
from taskloom.client.feedback_decision import request_decision
from taskloom.client.model_scheduler import ModelScheduler
from taskloom.client.ports import ExecutorPort
from taskloom.client.service import ClientService
from taskloom.contracts.models import (
    AccountLogin,
    ClientConfig,
    ModelReply,
    ModelRequest,
    PlatformConfig,
    RootRegistration,
    TaskCreate,
    ToolCall,
    ToolState,
    Usage,
)
from taskloom.domain.model import Outcome
from taskloom.gateway.service import GatewayService


def request(name, total=67):
    req = ModelRequest(
        request_id=name,
        root_task_id="root",
        agent_id="agent",
        step_id=name,
        attempt_id=name,
        messages=[{"role": "user", "content": "hello"}],
        deadline_at=(datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
        max_tokens=1,
    )
    req.max_tokens = total - req.input_tokens
    return req


class Provider:
    def __init__(self, price=1):
        self.price = price
        self.release = asyncio.Event()
        self.calls = []

    async def generate(self, req, account=None):
        self.calls.append(req.request_id)
        await self.release.wait()
        return ModelReply(
            content="done",
            usage=Usage(
                input_tokens=req.input_tokens,
                output_tokens=req.max_tokens,
                cost_units=math.ceil(req.input_tokens + req.max_tokens * self.price),
            ),
        )

    async def reconcile(self, request_id, account=None):
        return None

    async def close(self):
        pass


async def start(tmp_path, config, provider):
    store = SQLiteRepository(tmp_path / "platform.db")
    gateway = GatewayService(store, provider, config)
    await gateway.start()
    await gateway.accounts.register(AccountLogin(username="alice", password="password-12345"))
    await gateway.register_root("alice", RootRegistration(root_task_id="root"))
    return store, gateway


@pytest.mark.asyncio
async def test_l3_does_not_refuse_a_request_that_l4_can_afford(tmp_path):
    config = PlatformConfig(output_price=0.1, account_budget_units=30, total_budget_units=30)
    provider = Provider(price=0.1)
    provider.release.set()
    store, gateway = await start(tmp_path, config, provider)
    scheduler = ModelScheduler(
        max_inflight=1,
        coordinated=True,
        quantum=100,
        urgency_window=1,
        aging_window=100,
        feedback=lambda: gateway.feedback("alice"),
    )
    await scheduler.start()
    req = request("affordable")
    feedback = await gateway.feedback("alice")
    worst, tokens = feedback.admission_needs(req)
    assert worst <= feedback.account_admission_headroom_units

    async def execute(pending):
        row = await gateway.submit("alice", pending)
        async with asyncio.timeout(2):
            while row.state != "FINISHED":
                await asyncio.sleep(0.01)
                row = await gateway.query("alice", pending.request_id)
        return row

    try:
        print(
            {
                "token_upper_bound": tokens,
                "priced_budget_need": worst,
                "available_budget": feedback.account_admission_headroom_units,
            }
        )
        result = await asyncio.wait_for(scheduler.submit(req, execute), 2)
        assert result.consumed_units == worst
    finally:
        await scheduler.close()
        await gateway.close()
        await store.close()


@pytest.mark.asyncio
async def test_l3_uses_hard_headroom_instead_of_the_estimated_reservation(tmp_path):
    config = PlatformConfig(
        pool_capacity=2, account_max_inflight=2, account_budget_units=100, total_budget_units=100
    )
    provider = Provider()
    store, gateway = await start(tmp_path, config, provider)
    try:
        old = request("running", 70)
        gateway.estimator.settled(
            "learning", "alice", "agent", old.input_tokens, 1, old.input_tokens + 1, gateway.clock()
        )
        await gateway.submit("alice", old)
        async with asyncio.timeout(2):
            while not provider.calls:  # noqa: ASYNC110
                await asyncio.sleep(0.005)
        feedback = await gateway.feedback("alice")
        candidate = request("next", 50)
        worst, _ = feedback.admission_needs(candidate)
        print(
            {
                "estimated_remaining": feedback.account_remaining_units,
                "hard_headroom": feedback.account_admission_headroom_units,
                "candidate_worst": worst,
                "decision": request_decision(
                    feedback, reserve_units=candidate.reserve_units
                ).action,
            }
        )
        assert feedback.account_admission_headroom_units < worst <= feedback.account_remaining_units
        assert request_decision(feedback, reserve_units=candidate.reserve_units).action == "wait"
    finally:
        provider.release.set()
        await gateway.close()
        await store.close()


async def test_priced_agent_workflow_uses_the_same_debit_through_http(tmp_path):
    config = PlatformConfig(
        output_price=0.1,
        account_budget_units=900,
        total_budget_units=900,
    )
    app = platform_app(tmp_path / "server", config)
    platform = app.state.service
    await platform.provider.close()

    class WorkflowProvider(Provider):
        async def generate(self, req, account=None):
            self.calls.append(req.request_id)
            tools_done = any(message["role"] == "tool" for message in req.messages)
            return ModelReply(
                content=json.dumps({"files": 1, "words": 2, "summary": "verified"})
                if tools_done
                else "",
                tool_calls=[] if tools_done else [ToolCall(call_id="stats", name="file_stats")],
                usage=Usage(
                    input_tokens=req.input_tokens,
                    output_tokens=20,
                    cost_units=math.ceil(req.input_tokens + 20 * 0.1),
                ),
            )

    platform.provider = WorkflowProvider(price=0.1)
    async with app.router.lifespan_context(app):
        gateway = PlatformHTTP("http://platform")
        await gateway.client.aclose()
        gateway.client = httpx.AsyncClient(
            base_url="http://platform",
            transport=httpx.ASGITransport(app=app),
        )
        credential = await gateway.signup(AccountLogin(username="alice", password="password-12345"))
        gateway.client.headers["Authorization"] = "Bearer " + credential.api_key
        store = SQLiteRepository(tmp_path / "client.db")
        executor = AsyncMock(spec=ExecutorPort)
        executor.run.side_effect = lambda req: ToolState(
            request=req,
            quiescent=True,
            outcome=Outcome.SUCCEEDED,
            result={"files": 1, "words": 2},
        )
        client = ClientService(store, gateway, executor, tmp_path / "client", ClientConfig())
        client.models.settle = Mock(wraps=client.models.settle)
        await client.start()
        try:
            task = await client.create(TaskCreate(documents={"notes.txt": "one two"}))
            async with asyncio.timeout(4):
                while task.task_id in client.runners:  # noqa: ASYNC110
                    await asyncio.sleep(0.005)
            completed = client.get(task.task_id)
            assert completed.outcome == Outcome.SUCCEEDED, completed.error
            attempts = [a for a in client.attempts.values() if a.kind == "model"]
            assert len(attempts) == client.models.settle.call_count == 2
            for attempt, call in zip(attempts, client.models.settle.call_args_list, strict=True):
                req = ModelRequest.model_validate(attempt.request_payload)
                assert attempt.settled
                assert attempt.fair_charge_units == platform.worst(req) < req.reserve_units
                assert call.args == (
                    task.task_id,
                    attempt.fair_charge_units,
                    attempt.consumed_units,
                )
            assert completed.consumed_units == platform.quota.consumed_units <= 900
            assert completed.reserved_units == platform.quota.reserved_units == 0
        finally:
            await client.close()
            await store.close()
