"""模型请求调度器的单元测试。"""

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


async def test_settle_returned_credit_unblocks_the_next_request() -> None:
    """预留高于实付时，退回的额度要让下一个更贵的请求能发出去。

    ``quantum`` 是 100，第一条预留 90 的请求发完账上只剩 10。接着来一条
    预留 150 的请求：没有结转时一轮补到 110，不够；退回 80 之后一轮补到
    190，够了。不退回就是拿实际用量偏低当借口继续罚它。
    """
    scheduler = _scheduler(quantum=100)
    await scheduler.start()
    try:
        assert (await scheduler.submit(_request("r1", "root-a", cost=90), _record)).state
        scheduler.settle("root-a", 90, 10)
        record = await asyncio.wait_for(
            scheduler.submit(_request("r2", "root-a", cost=150), _record), timeout=2.0
        )
        assert record.request.request_id == "r2"
    finally:
        await scheduler.close()


def test_settle_rejects_negative_units() -> None:
    scheduler = _scheduler()
    with pytest.raises(ValueError, match="non-negative"):
        scheduler.settle("root-a", -1, 10)


async def test_callback_cost_share_is_fair_across_roots() -> None:
    """复现审查意见 2.1：按实际回调统计，两根任务的成本份额应当持平。

    单槽位、量子 100，一根任务每条请求成本 1000、另一根成本 20。按轮
    发放额度时，重的方约每十轮才发一条、轻的方每轮发两条多，长期看两边
    的服务成本应当相当；旧实现按「每次 pop 调用」发额度，重的方攒够额度
    之前轻的方已经把账面挤满了。
    """
    scheduler = _scheduler(max_inflight=1, quantum=100)
    await scheduler.start()
    served: list[ModelRequest] = []

    async def callback(request: ModelRequest) -> GatewayRecord:
        served.append(request)
        return _record(request)

    def feed(root: str, cost: int, count: int) -> list[Any]:
        return [
            asyncio.create_task(
                scheduler.submit(_request(f"{root}-{i}", root, cost=cost), callback)
            )
            for i in range(count)
        ]

    try:
        tasks = feed("heavy", 1000, 60) + feed("light", 20, 400)
        for _ in range(4000):
            if len(served) >= 200:
                break
            await asyncio.sleep(0.001)
        assert len(served) >= 200, len(served)
        units = {"heavy": 0, "light": 0}
        counts = {"heavy": 0, "light": 0}
        for request in served[:200]:
            units[request.root_task_id] += request.reserve_units
            counts[request.root_task_id] += 1
        assert counts["light"] > counts["heavy"], counts
        heavier = max(units.values())
        lighter = min(units.values())
        assert lighter > 0
        assert heavier / lighter < 1.6, (counts, units)
    finally:
        for task in tasks:
            task.cancel()
        await scheduler.close()


async def test_sustained_urgent_arrivals_do_not_starve_the_background() -> None:
    """复现审查意见 2.1 后半段：单槽位下持续涌入的紧急请求不能把后台挤掉。

    后台请求先等过老化阈值，随后紧急请求连绵不断。提升有上限、轮到次数
    跨调用保留，所以后台必须能通过正常轮转拿到派发机会。
    """
    scheduler = _scheduler(max_inflight=1, quantum=100, aging_window=0.05)
    await scheduler.start()
    order: list[str] = []

    async def callback(request: ModelRequest) -> GatewayRecord:
        order.append(request.request_id)
        return _record(request)

    tasks: list[Any] = []
    try:
        background = asyncio.create_task(
            scheduler.submit(_request("background", "bg", cost=20, deadline_seconds=600), callback)
        )
        tasks.append(background)
        await asyncio.sleep(0.08)
        for index in range(120):
            tasks.append(
                asyncio.create_task(
                    scheduler.submit(
                        _request(f"urgent-{index}", "urgent", cost=20, deadline_seconds=10),
                        callback,
                    )
                )
            )
        for _ in range(4000):
            if "background" in order:
                break
            await asyncio.sleep(0.001)
        assert "background" in order, order[:50]
        assert order.index("background") < 100
    finally:
        for task in tasks:
            task.cancel()
        await scheduler.close()


async def test_request_level_block_does_not_stop_other_roots() -> None:
    """复现审查意见 2.4 复现 A：一条请求暂时不可行时别的根任务照常派发。

    账户在途预留 9900、当前可用 100。队首要 200 的请求只能等，另一个
    根任务只要 50 的请求应当立刻发出去。
    """
    feedback = _feedback(
        account_budget_units=10_000,
        account_consumed_units=0,
        account_reserved_units=9_900,
        account_remaining_units=100,
    )
    scheduler = _scheduler(max_inflight=1, coordinated=True, feedback=lambda: feedback)
    await scheduler.start()
    served: list[str] = []

    async def callback(request: ModelRequest) -> GatewayRecord:
        served.append(request.request_id)
        return _record(request)

    big = asyncio.create_task(scheduler.submit(_request("big", "root-a", cost=200), callback))
    small = asyncio.create_task(scheduler.submit(_request("small", "root-b", cost=50), callback))
    try:
        await asyncio.wait_for(small, timeout=2.0)
        assert served == ["small"], served
        assert not big.done()
    finally:
        big.cancel()
        await scheduler.close()


async def test_deadline_wakes_the_backoff() -> None:
    """复现审查意见 2.4 复现 B：退避期间最早期限必须能把循环叫醒。

    平台要求 5 秒后再试，而请求 50 毫秒后到期；200 毫秒前就必须以
    ``deadline_expired`` 结束，而不是等退避睡完。
    """
    feedback = _feedback(can_submit=False, retry_after_seconds=5.0)
    scheduler = _scheduler(max_inflight=1, coordinated=True, feedback=lambda: feedback)
    await scheduler.start()
    try:
        with pytest.raises(Refused) as caught:
            await asyncio.wait_for(
                scheduler.submit(_request("r1", "root-a", deadline_seconds=0.05), _record),
                timeout=1.0,
            )
        assert caught.value.reason == DEADLINE_EXPIRED
    finally:
        await scheduler.close()


async def test_manual_priority_uses_model_scheduler_and_preserves_waiting_identity() -> None:
    scheduler = _scheduler(max_inflight=1)
    await scheduler.start()
    started, release = asyncio.Event(), asyncio.Event()
    seen: list[str] = []

    async def hold(request: ModelRequest) -> GatewayRecord:
        started.set()
        await release.wait()
        return _record(request)

    async def execute(request: ModelRequest) -> GatewayRecord:
        seen.append(request.request_id)
        return _record(request)

    try:
        first = asyncio.create_task(scheduler.submit(_request("hold", "hold"), hold))
        await started.wait()
        normal = asyncio.create_task(
            scheduler.submit(
                _request("normal", "normal"),
                execute,
                cost_units=100,
            )
        )
        changed = asyncio.create_task(
            scheduler.submit(
                _request("changed", "changed"),
                execute,
                cost_units=100,
                priority="low",
            )
        )
        await asyncio.sleep(0)
        assert scheduler.set_priority("changed", "high")
        assert not scheduler.set_priority("hold", "low")
        release.set()
        await asyncio.wait_for(asyncio.gather(first, normal, changed), 2)
        assert seen == ["changed", "normal"]
        assert scheduler.pending == 0 and scheduler.in_flight == 0
    finally:
        await scheduler.close()


async def test_manual_high_priority_cannot_starve_normal_cost_rotation() -> None:
    scheduler = _scheduler(max_inflight=1)
    await scheduler.start()
    started, release = asyncio.Event(), asyncio.Event()
    order: list[str] = []

    async def hold(request: ModelRequest) -> GatewayRecord:
        started.set()
        await release.wait()
        return _record(request)

    async def execute(request: ModelRequest) -> GatewayRecord:
        order.append(request.root_task_id)
        return _record(request)

    try:
        first = asyncio.create_task(scheduler.submit(_request("hold", "hold"), hold))
        await started.wait()
        high = [
            asyncio.create_task(
                scheduler.submit(
                    _request(f"high-{i}", "high"),
                    execute,
                    cost_units=100,
                    priority="high",
                )
            )
            for i in range(20)
        ]
        low = asyncio.create_task(
            scheduler.submit(
                _request("low", "low"),
                execute,
                cost_units=100,
                priority="low",
            )
        )
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(first, *high, low), 2)
        assert order.index("low") < 20
    finally:
        await scheduler.close()
