"""反馈准入判定的单元测试。"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from taskloom.client.feedback_decision import STALE_FEEDBACK_SECONDS, Decision, decide
from taskloom.contracts.models import PlatformFeedback


@pytest.fixture
def fresh_feedback() -> Callable[[], PlatformFeedback]:
    def make(**overrides: object) -> PlatformFeedback:
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

    return make


def test_uncoordinated_always_submits(fresh_feedback: Callable[[], PlatformFeedback]) -> None:
    feedback = fresh_feedback(can_submit=False)
    decision = decide(
        feedback,
        now=lambda: 0.0,
        in_flight=99,
        max_inflight=1,
        coordinated=False,
        has_queued=True,
        reserve_units=10_000_000,
    )
    assert decision == Decision("submit", "feedback_disabled")


def test_stale_feedback_waits(fresh_feedback: Callable[[], PlatformFeedback]) -> None:
    # observed_at 比陈旧阈值还老，把 now() 顶到远未来的纪元秒，
    # 这样时间差才是确定的。
    old = (datetime.now(UTC) - timedelta(seconds=STALE_FEEDBACK_SECONDS + 10)).isoformat()
    feedback = fresh_feedback(observed_at=old, retry_after_seconds=1.5)
    decision = decide(
        feedback,
        now=lambda: 1e12,
        in_flight=0,
        max_inflight=1,
        coordinated=True,
        has_queued=False,
        reserve_units=10,
    )
    assert decision.action == "wait"
    assert decision.reason == "stale_feedback"
    assert decision.retry_after == 1.5


def test_platform_block_returns_wait(fresh_feedback: Callable[[], PlatformFeedback]) -> None:
    feedback = fresh_feedback(can_submit=False, retry_after_seconds=2.5)
    decision = decide(
        feedback,
        now=lambda: 0.0,
        in_flight=0,
        max_inflight=1,
        coordinated=True,
        has_queued=False,
        reserve_units=10,
    )
    assert decision.action == "wait"
    assert decision.reason == "platform_blocked"
    assert decision.retry_after == 2.5


def test_local_backpressure_when_inflight_at_cap(
    fresh_feedback: Callable[[], PlatformFeedback],
) -> None:
    feedback = fresh_feedback(can_submit=True)
    decision = decide(
        feedback,
        now=lambda: 0.0,
        in_flight=1,
        max_inflight=1,
        coordinated=True,
        has_queued=False,
        reserve_units=10,
    )
    assert decision.action == "wait"
    assert decision.reason == "local_backpressure"


def test_insufficient_budget_refuses(fresh_feedback: Callable[[], PlatformFeedback]) -> None:
    """花不起钱是不可恢复的：预算减已消费都不够，直接拒绝。

    这条与 :meth:`ClientService.wait_platform` 的 ``ACCOUNT_BUDGET`` 判定
    同源，用同一组字段，避免两条路径对同一状态给出相反结论。
    """
    feedback = fresh_feedback(
        account_budget_units=200,
        account_consumed_units=100,
        account_remaining_units=100,
    )
    decision = decide(
        feedback,
        now=lambda: 0.0,
        in_flight=0,
        max_inflight=1,
        coordinated=True,
        has_queued=False,
        reserve_units=500,
    )
    assert decision.action == "refuse"
    assert decision.reason == "insufficient_budget"


def test_reserved_funds_wait_instead_of_refusing(
    fresh_feedback: Callable[[], PlatformFeedback],
) -> None:
    """剩余额度被在途预留占满只是暂时的资金占用，必须等待而不是拒绝。

    预算还够（``预算 - 已消费`` 远大于本次预留），只是 ``remaining_units``
    被别的在途请求预留压住。旧实现拿 ``remaining_units`` 判不可恢复，
    会把这种有救的情况直接判死。
    """
    feedback = fresh_feedback(
        account_budget_units=100_000,
        account_consumed_units=0,
        account_reserved_units=99_900,
        account_remaining_units=100,
        retry_after_seconds=0.3,
    )
    decision = decide(
        feedback,
        now=lambda: 0.0,
        in_flight=0,
        max_inflight=1,
        coordinated=True,
        has_queued=False,
        reserve_units=500,
    )
    assert decision.action == "wait"
    assert decision.reason == "budget_reserved"
    assert decision.retry_after == 0.3


def test_window_token_shortfall_waits(fresh_feedback: Callable[[], PlatformFeedback]) -> None:
    """窗口 token 不够只是速率窗口问题，等到下一个窗口即可，不是拒绝。"""
    feedback = fresh_feedback(account_window_tokens_remaining=100, retry_after_seconds=0.2)
    decision = decide(
        feedback,
        now=lambda: 0.0,
        in_flight=0,
        max_inflight=1,
        coordinated=True,
        has_queued=False,
        reserve_units=500,
    )
    assert decision.action == "wait"
    assert decision.reason == "window_exhausted"


def test_real_gateway_composition_waits_on_account_queue(
    fresh_feedback: Callable[[], PlatformFeedback],
) -> None:
    """按真实 ``GatewayService.feedback`` 的组合条件造一条反馈。

    账户已有一条排队时平台把 ``can_submit`` 置为 ``False``，此时必须走
    可恢复等待，而不是被判成花不起钱。
    """
    feedback = fresh_feedback(
        account_queued=1,
        can_submit=False,
        recommended_pending_limit=1,
        retry_after_seconds=0.2,
    )
    decision = decide(
        feedback,
        now=lambda: 0.0,
        in_flight=0,
        max_inflight=1,
        coordinated=True,
        has_queued=True,
        reserve_units=100,
    )
    assert decision == Decision("wait", "platform_blocked", 0.2)


def test_happy_path_submits(fresh_feedback: Callable[[], PlatformFeedback]) -> None:
    feedback = fresh_feedback()
    decision = decide(
        feedback,
        now=lambda: 0.0,
        in_flight=0,
        max_inflight=1,
        coordinated=True,
        has_queued=False,
        reserve_units=100,
    )
    assert decision == Decision("submit", "platform_open")


def test_platform_queued_full_waits(fresh_feedback: Callable[[], PlatformFeedback]) -> None:
    feedback = fresh_feedback(
        account_queued=1, recommended_pending_limit=0, retry_after_seconds=0.4
    )
    decision = decide(
        feedback,
        now=lambda: 0.0,
        in_flight=0,
        max_inflight=1,
        coordinated=True,
        has_queued=True,
        reserve_units=10,
    )
    assert decision.action == "wait"
    assert decision.reason == "platform_queued_full"
    assert decision.retry_after == 0.4
