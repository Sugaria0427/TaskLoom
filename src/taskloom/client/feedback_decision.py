"""判定一条模型请求该提交、该等待还是该拒绝。

判定输入是一份平台反馈快照，加上几个 L3 本地事实（当前在途数、账户是否
已经排着队、候选请求的预留额度），输出 :class:`Decision`。调用方把判定
翻译成对应的 ``asyncio`` 原语：立即提交、按 ``retry_after`` 睡一会儿、
或者拿 ``reason`` 当面向用户的错误码拒绝。

判定本身从不访问平台，它是入参的纯函数。这样它可确定性测试，也符合
WP3 的设计边界：L3 只读反馈，不与平台协商。

示例:
    >>> from taskloom.contracts.models import PlatformFeedback
    >>> from datetime import datetime, timezone
    >>> feedback = PlatformFeedback(
    ...     platform_id="p1", account_id="a1", model="m",
    ...     pool_capacity=2, pool_inflight=0,
    ...     platform_queued=0, platform_remaining_units=1_000_000,
    ...     account_budget_units=100_000, account_consumed_units=0,
    ...     account_reserved_units=0, account_remaining_units=100_000,
    ...     account_inflight=0, account_max_inflight=1, account_queued=0,
    ...     account_window_tokens_remaining=50_000,
    ...     can_submit=True, retry_after_seconds=0.0,
    ...     observed_at=datetime.now(timezone.utc).isoformat(),
    ... )
    >>> decide(feedback, in_flight=0, max_inflight=1,
    ...         coordinated=True, has_queued=False, reserve_units=100,
    ...         now=lambda: 0.0).action
    'submit'
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from taskloom.contracts.models import PlatformFeedback

STALE_FEEDBACK_SECONDS = 5.0


@dataclass(frozen=True)
class Decision:
    """单次模型请求尝试的调度判定。

    Attributes:
        action: ``"submit"``、``"wait"``、``"refuse"`` 三者之一。
        reason: 短稳定串，当作错误码或日志键；``"submit"`` 的取值见
            :func:`decide`。
        retry_after: ``action == "wait"`` 时调用方该睡的秒数，其余情况为零。
    """

    action: Literal["submit", "wait", "refuse"]
    reason: str
    retry_after: float = 0.0


def decide(
    feedback: PlatformFeedback,
    *,
    now: Callable[[], float],
    in_flight: int,
    max_inflight: int,
    coordinated: bool,
    has_queued: bool,
    reserve_units: int,
) -> Decision:
    """判定一个候选模型请求 L3 该做什么。

    Args:
        feedback: 本地账户最近一份平台反馈快照，是平台准入与排队状态的
            权威来源。
        now: 返回当前纪元秒的函数，用来判断快照新鲜度。
        in_flight: 本机当前在途的 L3 模型请求数。
        max_inflight: 本机在途模型请求的并发上限，与
            ``ClientConfig.max_inflight`` 对齐。
        coordinated: 置 ``False`` 时本地对照基线直接放行；调用方仍要为
            自己想保留的预算检查负责。
        has_queued: 快照之后账户是否已经排着队。
        reserve_units: 候选请求在账户账本上要预留的成本。先与账户未花预算
            比（不可恢复的缺口），再与 ``account_remaining_units`` 和
            窗口 token 额度比（两者都可恢复）。

    Returns:
        :class:`Decision`，其 ``action`` 为
        ``"submit"``、``"wait"``、``"refuse"`` 之一。

    Note:
        判据与 :meth:`ClientService.wait_platform` 逐条对齐，避免同一个
        平台状态在两条路径上得到相反结论。

        * 不可恢复的费用不足只看 ``预算 - 已消费``，与 ``ACCOUNT_BUDGET``
          的既有语义一致，直接拒绝。
        * ``account_remaining_units`` 还要扣在途预留，它不足只是暂时的
          资金占用，不能当成花不起钱；窗口 token 额度同理。两者都走
          可恢复等待。
        * 账户预算耗尽时平台同样把 ``can_submit`` 置为 ``False``，所以
          不可恢复判定必须排在它前面，否则会白等下去。
    """
    if not coordinated:
        return Decision("submit", "feedback_disabled")
    age = max(0.0, now() - _observed_epoch(feedback))
    if age > STALE_FEEDBACK_SECONDS:
        return Decision("wait", "stale_feedback", feedback.retry_after_seconds)
    spent_headroom = feedback.account_budget_units - feedback.account_consumed_units
    if spent_headroom < reserve_units:
        return Decision("refuse", "insufficient_budget")
    if in_flight >= max_inflight:
        return Decision("wait", "local_backpressure", feedback.retry_after_seconds or 0.5)
    if not feedback.can_submit:
        return Decision("wait", "platform_blocked", feedback.retry_after_seconds)
    if feedback.account_remaining_units < reserve_units:
        return Decision("wait", "budget_reserved", feedback.retry_after_seconds)
    if feedback.account_window_tokens_remaining < reserve_units:
        return Decision("wait", "window_exhausted", feedback.retry_after_seconds)
    pending_limit = max(1, feedback.recommended_pending_limit)
    if has_queued and feedback.account_queued >= pending_limit:
        return Decision("wait", "platform_queued_full", feedback.retry_after_seconds)
    return Decision("submit", "platform_open")


def _observed_epoch(feedback: PlatformFeedback) -> float:
    """把反馈里的 ISO 时间戳换成纪元秒。"""
    from datetime import datetime

    parsed = datetime.fromisoformat(feedback.observed_at)
    return parsed.timestamp()
