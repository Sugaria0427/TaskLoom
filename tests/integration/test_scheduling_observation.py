"""Observing the OS scheduling tree must neither dispatch work nor change fair turns."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx

from taskloom.bootstrap.apps import client_app
from taskloom.client.dispatcher import Dispatcher
from taskloom.client.model_scheduler import ModelScheduler
from taskloom.contracts.models import ClientConfig, GatewayRecord, ModelRequest, TaskCreate
from taskloom.domain.model import Resources
from taskloom.policies.aging import AgingQueue


def test_preview_preserves_credit_promotion_and_real_dispatch_order():
    now = 1000.0
    queue = AgingQueue(
        quantum=10,
        cost=lambda item: item[1],
        deadline=lambda item: item[2],
        now=lambda: now,
        urgency_window=5,
        aging_window=20,
        urgency_burst=1,
    )
    queue.push("expensive", ("heavy", 40, 1200))
    queue.push("urgent", ("urgent", 10, 1001))
    queue.push("small", ("small", 5, 1200))
    before = queue.entries()
    for _ in range(20):
        candidate, reason = queue.preview()
        assert candidate[0] == "urgent" and reason == "deadline"
        assert queue.entries() == before
        assert queue.deficit("urgent") == 0
    assert queue.pop(limit=1)[0][0] == "urgent"
    queue.push("urgent", ("later", 10, 1200))
    assert queue.deficit("urgent") == -10
    candidate, reason = queue.preview()
    assert candidate[0] == "small" and reason == "cost_drr"
    assert queue.pop(limit=1)[0] == candidate


async def test_tool_prediction_is_conditional_and_never_reserves_resources():
    dispatcher = Dispatcher(Resources(slots=1, memory_bytes=256))
    dispatcher.pool.reserve("busy", Resources(slots=1, memory_bytes=256))
    called = False

    async def execute():
        nonlocal called
        called = True

    pending = asyncio.create_task(
        dispatcher.submit("next", "root", Resources(slots=1, memory_bytes=128), execute)
    )
    await asyncio.sleep(0)
    try:
        for _ in range(20):
            observed = dispatcher.observation()
            assert observed["next_attempt_id"] == "next"
            assert observed["conditional"]
            assert observed["entries"][0]["blocked_by"]
        assert not called
        assert dispatcher.pool.available == Resources()
        assert len(dispatcher) == 1
        assert dispatcher._turn == 0
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await dispatcher.close()


async def test_model_prediction_matches_next_actual_request_without_advancing_queue(
    platform_feedback,
):
    release = asyncio.Event()
    entered = asyncio.Event()
    calls = []
    scheduler = ModelScheduler(
        max_inflight=1,
        coordinated=True,
        quantum=100,
        urgency_window=1,
        aging_window=100,
        feedback=lambda: platform_feedback,
    )
    await scheduler.start()

    async def execute(request):
        calls.append(request.request_id)
        entered.set()
        await release.wait()
        return GatewayRecord(tenant_id="alice", request=request, payload_hash="x")

    def request(key):
        return ModelRequest(
            request_id=key,
            attempt_id=key,
            root_task_id=key,
            agent_id="agent",
            step_id=key,
            messages=[{"role": "user", "content": "test"}],
            max_tokens=1,
            deadline_at=(datetime.now(UTC) + timedelta(seconds=300)).isoformat(),
        )

    jobs = [
        asyncio.create_task(scheduler.submit(request(key), execute))
        for key in ("first", "second", "third")
    ]
    try:
        async with asyncio.timeout(2):
            await entered.wait()
        for _ in range(20):
            observed = scheduler.observation(platform_feedback)
            assert observed["next_attempt_id"] == "second"
            assert observed["blocked_by"] == "local_backpressure"
        stale = platform_feedback.model_copy(
            update={"observed_at": (datetime.now(UTC) - timedelta(seconds=30)).isoformat()}
        )
        assert scheduler.observation(stale)["next_attempt_id"] is None
        assert calls == ["first"] and scheduler.pending == 2
        release.set()
        await asyncio.gather(*jobs)
        assert calls == ["first", "second", "third"]
    finally:
        await scheduler.close()
        await asyncio.gather(*jobs, return_exceptions=True)


async def test_live_resource_endpoint_exposes_timing_and_dependencies_without_rescheduling(
    tmp_path,
):
    app = client_app(tmp_path, ClientConfig())
    service = app.state.service
    # Create a pending task directly: no external provider or executor is contacted.
    from taskloom.contracts.models import TaskState
    from taskloom.domain.identity import utc_now
    from taskloom.domain.model import WorkState

    task = TaskState(
        task_id="task",
        tenant_id="alice",
        agent_id="agent",
        input_version="v1",
        specification=TaskCreate(),
        deadline_at=utc_now(),
    )
    service.tasks[task.task_id] = task
    try:
        first = await service._attempt("task", "task-model-0", "model")
        assert first.created_at and first.state_changed_at
        first.state = WorkState.RUNNING
        await service._save_attempt(first)
        persisted = service.attempts[first.attempt_id].model_copy(deep=True)
        assert persisted.dispatched_at
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for _ in range(3):
                response = await client.get("/api/v1/resources")
                assert response.status_code == 200
                snapshot = response.json()
                assert snapshot["scheduling"]["model"]["next_attempt_id"] is None
                assert snapshot["attempts"][0]["dispatched_at"] == persisted.dispatched_at
                assert snapshot["attempts"][0]["predecessors"] == []
            page = await client.get("/")
            assert 'role="tablist"' in page.text and 'id="workflow-canvas"' in page.text
            assert (await client.get("/static/scheduler-tree.js")).status_code == 200
        assert service.attempts[first.attempt_id] == persisted
        assert service.resources()["allocations"] == []
        from taskloom.domain.model import Outcome

        persisted.state = WorkState.FINISHED
        persisted.outcome = Outcome.SUCCEEDED
        persisted.quiescent = True
        persisted.settled = True
        await service._save_attempt(persisted)
        tool = await service._attempt(
            "task",
            "task-model-0-tool-call",
            "tool",
            {"tool_name": "file_stats", "arguments": {}},
            model_round=0,
        )
        assert tool.predecessors == ["task-model-0"]
        saved = await service.store.get("attempt", first.attempt_id)
        assert saved["created_at"] <= saved["dispatched_at"] <= saved["completed_at"]
    finally:
        await service.close()
        await service.store.close()
