"""Unit tests for :mod:`taskloom.client.model_scheduler`."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from taskloom.client.model_scheduler import (
    DEADLINE_EXPIRED,
    ModelScheduler,
    Refused,
)
from taskloom.contracts.models import (
    GatewayRecord,
    ModelReply,
    ModelRequest,
    Outcome,
    PlatformFeedback,
    Usage,
)


def _request(
    request_id: str, root: str, cost: int = 100, deadline_seconds: float = 600.0
) -> ModelRequest:
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
        state="QUEUED",
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


def _scheduler(**overrides: Any) -> ModelScheduler:
    base: dict[str, Any] = dict(
        max_inflight=2,
        coordinated=False,
        quantum=100,
        urgency_window=60.0,
        aging_window=20.0,
        feedback=lambda: _feedback(),
    )
    base.update(overrides)
    return ModelScheduler(**base)


async def test_uncoordinated_dispatches_immediately() -> None:
    scheduler = _scheduler()
    await scheduler.start()
    try:
        records = await asyncio.gather(
            scheduler.submit(_request("r1", "root-a"), _record),
            scheduler.submit(_request("r2", "root-b"), _record),
        )
        assert [r.request.request_id for r in records] == ["r1", "r2"]
    finally:
        await scheduler.close()


async def test_capacity_allows_true_parallelism() -> None:
    """复现审查意见 2.2：``max_inflight=2`` 必须真的并行。

    第一个回调卡在释放事件上，第二个必须能在它结束前就开跑；旧实现
    主循环 ``await`` 整个回调，第二个永远排在第一个后面。
    """
    release = asyncio.Event()
    running = 0
    peak = 0
    lock = asyncio.Lock()

    async def slow(_request: ModelRequest) -> GatewayRecord:
        nonlocal running, peak
        async with lock:
            running += 1
            peak = max(peak, running)
        await release.wait()
        async with lock:
            running -= 1
        return _record(_request)

    scheduler = _scheduler(max_inflight=2)
    await scheduler.start()
    try:
        first = asyncio.create_task(scheduler.submit(_request("r1", "root-a"), slow))
        second = asyncio.create_task(scheduler.submit(_request("r2", "root-b"), slow))
        await asyncio.sleep(0.05)
        assert scheduler.in_flight == 2
        assert peak == 2
        release.set()
        await asyncio.gather(first, second)
    finally:
        await scheduler.close()


async def test_inflight_cap_is_respected() -> None:
    release = asyncio.Event()
    started = 0

    async def slow(_request: ModelRequest) -> GatewayRecord:
        nonlocal started
        started += 1
        await release.wait()
        return _record(_request)

    scheduler = _scheduler(max_inflight=1)
    await scheduler.start()
    tasks = [
        asyncio.create_task(scheduler.submit(_request(f"r{i}", f"root-{i}"), slow))
        for i in range(4)
    ]
    await asyncio.sleep(0.05)
    assert scheduler.in_flight == 1
    release.set()
    await asyncio.gather(*tasks)
    assert scheduler.in_flight == 0
    await scheduler.close()


async def test_feedback_read_failure_keeps_the_candidate_owned() -> None:
    """复现审查意见 2.3：反馈读取异常不能留下悬挂请求。

    反馈第一次抛 ``ConnectionError``，之后恢复正常。请求必须留在
    调度器手上：先带着原来的等待时长回队，重试够了次数仍失败就把
    异常交给调用者，两种结局都不允许无限期挂着。
    """
    reads = 0

    def flaky() -> PlatformFeedback:
        nonlocal reads
        reads += 1
        if reads == 1:
            raise ConnectionError("feedback down")
        return _feedback()

    scheduler = _scheduler(feedback=flaky)
    await scheduler.start()
    try:
        record = await asyncio.wait_for(
            scheduler.submit(_request("r1", "root-a"), _record), timeout=2.0
        )
        assert record.request.request_id == "r1"
        assert reads >= 2
        assert scheduler.pending == 0
        assert scheduler.in_flight == 0
    finally:
        await scheduler.close()


async def test_permanent_feedback_failure_reaches_the_caller() -> None:
    """反馈一直失败时，请求不能无限重试，必须以异常收场。"""
    reads = 0

    def broken() -> PlatformFeedback:
        nonlocal reads
        reads += 1
        raise ConnectionError("feedback down")

    scheduler = _scheduler(feedback=broken)
    await scheduler.start()
    try:
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(scheduler.submit(_request("r1", "root-a"), _record), timeout=2.0)
        assert reads >= 3
        assert scheduler.pending == 0
    finally:
        await scheduler.close()


async def test_awaitable_returning_sync_feedback_is_awaited() -> None:
    """审查意见 2.3 的补充：同步函数返回 awaitable 时也要按返回值 await。

    按 ``iscoroutinefunction`` 判函数类型会漏掉这种合法写法，未 await 的
    协程被当成反馈对象，紧接着读字段就抛 ``AttributeError``。
    """
    reads = 0

    def provider() -> Any:
        nonlocal reads
        reads += 1

        async def read() -> PlatformFeedback:
            return _feedback()

        return read()

    scheduler = _scheduler(coordinated=True, feedback=provider)
    await scheduler.start()
    try:
        record = await asyncio.wait_for(
            scheduler.submit(_request("r1", "root-a"), _record), timeout=2.0
        )
        assert record.request.request_id == "r1"
        assert reads == 1
        assert scheduler.pending == 0
    finally:
        await scheduler.close()


async def test_expired_request_reaches_a_terminal_state() -> None:
    """复现审查意见 2.3：过期是终态，调用者要收到错误且队列要清干净。"""
    scheduler = _scheduler()
    await scheduler.start()
    try:
        with pytest.raises(Refused) as caught:
            await asyncio.wait_for(
                scheduler.submit(_request("r1", "root-a", deadline_seconds=-1.0), _record),
                timeout=2.0,
            )
        assert caught.value.reason == DEADLINE_EXPIRED
        assert scheduler.pending == 0
        assert scheduler.in_flight == 0
    finally:
        await scheduler.close()


async def test_request_expiring_while_waiting_is_swept() -> None:
    """等在队列里期间到期的请求，要有定时唤醒把它收尾。"""
    blocker = asyncio.Event()

    async def hold(_request: ModelRequest) -> GatewayRecord:
        await blocker.wait()
        return _record(_request)

    scheduler = _scheduler(max_inflight=1)
    await scheduler.start()
    first = asyncio.create_task(scheduler.submit(_request("r1", "root-a"), hold))
    await asyncio.sleep(0.02)
    expiring = asyncio.create_task(
        scheduler.submit(_request("r2", "root-b", deadline_seconds=0.1), _record)
    )
    with pytest.raises(Refused) as caught:
        await asyncio.wait_for(expiring, timeout=2.0)
    assert caught.value.reason == DEADLINE_EXPIRED
    assert scheduler.pending == 0
    blocker.set()
    await first
    await scheduler.close()


async def test_insufficient_budget_refuses() -> None:
    """复现审查意见 2.4：预算耗尽要拒绝，不能当成平台阻塞去等。

    账户预算与已消费同为 50、预留 0、剩余 0 时，真实网关算出的
    ``can_submit`` 也是 ``False``，所以旧逻辑会先命中平台阻塞分支。
    """
    scheduler = _scheduler(
        coordinated=True,
        feedback=lambda: _feedback(
            account_budget_units=50,
            account_consumed_units=50,
            account_remaining_units=0,
            can_submit=False,
        ),
    )
    await scheduler.start()
    try:
        with pytest.raises(Refused) as caught:
            await asyncio.wait_for(
                scheduler.submit(_request("r1", "root-a", cost=500), _record),
                timeout=2.0,
            )
        assert caught.value.reason == "insufficient_budget"
    finally:
        await scheduler.close()


async def test_cancel_removes_pending_request() -> None:
    blocker = asyncio.Event()

    async def hold(_request: ModelRequest) -> GatewayRecord:
        await blocker.wait()
        return _record(_request)

    scheduler = _scheduler(max_inflight=1)
    await scheduler.start()
    first = asyncio.create_task(scheduler.submit(_request("r1", "root-a"), hold))
    await asyncio.sleep(0.02)
    second = asyncio.create_task(scheduler.submit(_request("r2", "root-b"), hold))
    await asyncio.sleep(0.02)
    assert await scheduler.cancel("r2") is True
    with pytest.raises(asyncio.CancelledError):
        await second
    assert await scheduler.cancel("r2") is False
    assert await scheduler.cancel("missing") is False
    blocker.set()
    await first
    await scheduler.close()


async def test_close_cancels_pending_and_in_flight() -> None:
    started = asyncio.Event()
    blocker = asyncio.Event()

    async def hold(_request: ModelRequest) -> GatewayRecord:
        started.set()
        await blocker.wait()
        return _record(_request)

    scheduler = _scheduler(max_inflight=1)
    await scheduler.start()
    first = asyncio.create_task(scheduler.submit(_request("r1", "root-a"), hold))
    await started.wait()
    second = asyncio.create_task(scheduler.submit(_request("r2", "root-b"), hold))
    await asyncio.sleep(0.02)
    await scheduler.close()
    with pytest.raises(asyncio.CancelledError):
        await first
    with pytest.raises(asyncio.CancelledError):
        await second


async def test_submit_before_start_is_rejected() -> None:
    scheduler = _scheduler()
    with pytest.raises(RuntimeError):
        await scheduler.submit(_request("r1", "root-a"), _record)


async def test_submit_after_close_is_rejected() -> None:
    """关掉调度器之后再提交必须被拒，不能让调用者永久挂在 future 上。"""
    scheduler = _scheduler()
    await scheduler.start()
    await scheduler.close()
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(scheduler.submit(_request("r1", "root-a"), _record), timeout=1.0)


async def test_awaitable_returning_sync_callback_is_awaited() -> None:
    """同步 callback 返回 awaitable 时也必须 await，否则 future 拿到的是协程。"""
    scheduler = _scheduler()

    def callback(request: ModelRequest) -> Any:
        async def read() -> GatewayRecord:
            return _record(request)

        return read()

    await scheduler.start()
    try:
        record = await asyncio.wait_for(
            scheduler.submit(_request("r1", "root-a"), callback), timeout=2.0
        )
        assert isinstance(record, GatewayRecord)
        assert record.request.request_id == "r1"
    finally:
        await scheduler.close()
