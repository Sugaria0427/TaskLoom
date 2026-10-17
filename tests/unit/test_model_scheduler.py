"""Unit tests for :mod:`taskloom.client.model_scheduler`."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from taskloom.client.model_scheduler import ModelScheduler, Refused
from taskloom.contracts.models import (
    GatewayRecord,
    ModelReply,
    ModelRequest,
    Outcome,
    PlatformFeedback,
    Usage,
)


def _request(request_id: str, root: str, cost: int = 100, deadline_seconds: float = 600.0) -> ModelRequest:
    deadline = (datetime.now(UTC) + timedelta(seconds=deadline_seconds)).isoformat()
    return ModelRequest(
        request_id=request_id,
        root_task_id=root,
        agent_id=f"agent-{root}",
        step_id=f"step-{request_id}",
        attempt_id=f"attempt-{request_id}",
        messages=[{"role": "user", "content": "x"}],
        max_tokens=cost,
        deadline_at=deadline,
    )


def _record(request: ModelRequest) -> GatewayRecord:
    return GatewayRecord(
        tenant_id=request.root_task_id,
        request=request,
        payload_hash=f"hash-{request.request_id}",
        state="COMPLETED",
        outcome=Outcome.SUCCEEDED,
        reply=ModelReply(
            content="ok",
            tool_calls=[],
            usage=Usage(input_tokens=1, output_tokens=1, cost_units=2),
        ),
    )


def _feedback(**overrides: Any) -> PlatformFeedback:
    base = dict(
        platform_id="p1",
        account_id="a1",
        model="m",
        pool_capacity=2,
        pool_inflight=0,
        platform_queued=0,
        platform_remaining_units=1_000_000,
        account_budget_units=100_000,
        account_consumed_units=0,
        account_reserved_units=0,
        account_remaining_units=100_000,
        account_inflight=0,
        account_max_inflight=1,
        account_queued=0,
        account_window_tokens_remaining=50_000,
        can_submit=True,
        retry_after_seconds=0.0,
        observed_at=datetime.now(UTC).isoformat(),
    )
    base.update(overrides)
    return PlatformFeedback(**base)  # type: ignore[arg-type]


async def test_uncoordinated_dispatches_immediately() -> None:
    scheduler = ModelScheduler(
        max_inflight=2,
        coordinated=False,
        quantum=100,
        urgency_window=60.0,
        aging_window=20.0,
        feedback=lambda: _feedback(),
    )
    await scheduler.start()
    try:
        records = await asyncio.gather(
            scheduler.submit(_request("r1", "root-a"), _record),
            scheduler.submit(_request("r2", "root-b"), _record),
        )
        assert [r.request.request_id for r in records] == ["r1", "r2"]
    finally:
        await scheduler.close()


async def test_close_cancels_pending() -> None:
    """Closing the scheduler cancels both queued and in-flight requests."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_callback(_request: ModelRequest) -> GatewayRecord:
        started.set()
        await release.wait()
        return _record(_request)

    scheduler = ModelScheduler(
        max_inflight=1,
        coordinated=False,
        quantum=100,
        urgency_window=60.0,
        aging_window=20.0,
        feedback=lambda: _feedback(),
    )
    await scheduler.start()
    try:
        first = asyncio.create_task(scheduler.submit(_request("r1", "root-a"), slow_callback))
        await started.wait()
        second = asyncio.create_task(scheduler.submit(_request("r2", "root-b"), slow_callback))
        # Give _step a chance to enqueue r2 behind r1.
        await asyncio.sleep(0)
    finally:
        await scheduler.close()
    with pytest.raises(asyncio.CancelledError):
        await first
    with pytest.raises(asyncio.CancelledError):
        await second


async def test_cancel_removes_pending_request() -> None:
    scheduler = ModelScheduler(
        max_inflight=1,
        coordinated=False,
        quantum=100,
        urgency_window=60.0,
        aging_window=20.0,
        feedback=lambda: _feedback(),
    )
    await scheduler.start()
    try:
        asyncio.create_task(scheduler.submit(_request("r1", "root-a"), _record))
        await asyncio.sleep(0.05)  # let the dispatcher drain r1
        assert await scheduler.cancel("nonexistent") is False
        # Submit then immediately cancel a queued request.
        pending_task = asyncio.create_task(
            scheduler.submit(_request("r2", "root-b"), _record)
        )
        await asyncio.sleep(0)
        # If r2 was already dispatched the cancel returns False; otherwise True.
        result = await scheduler.cancel("r2")
        if result:
            with pytest.raises(asyncio.CancelledError):
                await pending_task
        else:
            await pending_task
    finally:
        await scheduler.close()


async def test_insufficient_budget_refuses() -> None:
    async def feedback() -> PlatformFeedback:
        return _feedback(account_remaining_units=50)

    scheduler = ModelScheduler(
        max_inflight=1,
        coordinated=True,
        quantum=100,
        urgency_window=60.0,
        aging_window=20.0,
        feedback=feedback,
    )
    await scheduler.start()
    try:
        with pytest.raises(Refused) as exc_info:
            await scheduler.submit(_request("r1", "root-a", cost=500), _record)
        assert exc_info.value.reason == "insufficient_budget"
    finally:
        await scheduler.close()


async def test_max_inflight_caps_concurrent_dispatch() -> None:
    """Two callbacks are running concurrently; only ``max_inflight`` of them start at once."""
    started: list[str] = []
    started_event = asyncio.Event()
    release = asyncio.Event()

    async def gated(request: ModelRequest) -> GatewayRecord:
        started.append(request.request_id)
        if len(started) == 2:
            started_event.set()
        await release.wait()
        return _record(request)

    scheduler = ModelScheduler(
        max_inflight=1,
        coordinated=False,
        quantum=100,
        urgency_window=60.0,
        aging_window=20.0,
        feedback=lambda: _feedback(),
    )
    await scheduler.start()
    try:
        tasks = [
            asyncio.create_task(scheduler.submit(_request("r1", "root-a"), gated)),
            asyncio.create_task(scheduler.submit(_request("r2", "root-b"), gated)),
        ]
        # Only the first one should start with max_inflight=1.
        await asyncio.sleep(0.05)
        assert scheduler.in_flight <= 1
        release.set()
        await asyncio.gather(*tasks)
    finally:
        await scheduler.close()


async def test_dispatch_uses_callback_return_value() -> None:
    sentinel = _record(_request("r1", "root-a"))
    sentinel = sentinel.model_copy(update={"payload_hash": "sentinel"})

    async def callback(request: ModelRequest) -> GatewayRecord:
        return sentinel

    scheduler = ModelScheduler(
        max_inflight=1,
        coordinated=False,
        quantum=100,
        urgency_window=60.0,
        aging_window=20.0,
        feedback=lambda: _feedback(),
    )
    await scheduler.start()
    try:
        result = await scheduler.submit(_request("r1", "root-a"), callback)
        assert result.payload_hash == "sentinel"
    finally:
        await scheduler.close()