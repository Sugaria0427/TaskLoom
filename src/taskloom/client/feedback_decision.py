"""判定 L3 模型请求该提交、该等待还是该拒绝。

判定分成两层：

* :func:`account_decision` 看账户级事实——快照是不是够新、本机在途是不
  是已经顶满、平台让不让进、账户自己的队列是不是已经排满。这些结论与
  具体请求无关，一旦成立所有候选都得等。
* :func:`request_decision` 只看这一条请求自己的额度：永久付不起就直接
  拒绝，暂时被占用就等一会儿再说。

分开的理由是阻塞范围不同：账户级阻塞要让整个调度循环退避，请求级阻塞
只该挡住这一条，别的根任务照常派发。两者混在一起时，一条凑不齐额度的大
请求会把同队列里能跑的小请求一起拖住。

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
    >>> account_decision(feedback, in_flight=0, max_inflight=1,
    ...                  coordinated=True, has_queued=False,
    ...                  now=lambda: datetime.now(timezone.utc).timestamp()).action
    'submit'
    >>> request_decision(feedback, reserve_units=100).action
    'submit'
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from taskloom.contracts.models import PlatformFeedback

STALE_FEEDBACK_SECONDS = 5.0


@dataclass(frozen=True)
class Decision:
    """一次判定的结论。

    Attributes:
        action: ``"submit"``、``"wait"``、``"refuse"`` 三者之一。
        reason: 短稳定串，当作错误码或日志键。
        retry_after: ``action == "wait"`` 时调用方该睡的秒数，其余情况为零。
    """

    action: Literal["submit", "wait", "refuse"]
    reason: str
    retry_after: float = 0.0


def account_decision(
    feedback: PlatformFeedback,
    *,
    now: Callable[[], float],
    in_flight: int,
    max_inflight: int,
    coordinated: bool,
    has_queued: bool,
) -> Decision:
    """判定账户级状态是否允许继续派发。

    Args:
        feedback: 本地账户最近一份平台反馈快照。
        now: 返回当前纪元秒的函数，用来判断快照新鲜度。
        in_flight: 本机当前在途的 L3 模型请求数。
        max_inflight: 本机在途模型请求的并发上限，与
            ``ClientConfig.max_inflight`` 对齐。
        coordinated: 置 ``False`` 时本地对照基线直接放行。
        has_queued: 快照之后账户是否已经排着队。

    Returns:
        ``"wait"`` 表示整个调度循环该退避；``"submit"`` 表示账户层面没有
        阻塞，可以继续看具体请求。
    """
    if not coordinated:
        return Decision("submit", "feedback_disabled")
    if stale_feedback(feedback, now=now):
        return Decision("wait", "stale_feedback", feedback.retry_after_seconds)
    if in_flight >= max_inflight:
        return Decision("wait", "local_backpressure", feedback.retry_after_seconds or 0.5)
    if not feedback.can_submit:
        return Decision("wait", "platform_blocked", feedback.retry_after_seconds)
    pending_limit = max(1, feedback.recommended_pending_limit)
    if has_queued and feedback.account_queued >= pending_limit:
        return Decision("wait", "platform_queued_full", feedback.retry_after_seconds)
    return Decision("submit", "account_open")


def request_decision(feedback: PlatformFeedback, *, reserve_units: int) -> Decision:
    """判定一条候选请求自己够不够额度。

    Args:
        feedback: 平台反馈快照。
        reserve_units: 候选请求在账户账本上要预留的成本。

    Returns:
        ``"refuse"`` 表示账户余额已经付不起这一条（不可恢复）；
        ``"wait"`` 表示只是暂时被别的在途请求占着；``"submit"`` 表示能发。
    """
    spent_headroom = feedback.account_budget_units - feedback.account_consumed_units
    if spent_headroom < reserve_units:
        return Decision("refuse", "insufficient_budget")
    if feedback.account_remaining_units < reserve_units:
        return Decision("wait", "budget_reserved", feedback.retry_after_seconds)
    if feedback.account_window_tokens_remaining < reserve_units:
        return Decision("wait", "window_exhausted", feedback.retry_after_seconds)
    return Decision("submit", "request_open")


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
    """把账户级与请求级判定合成一个结论。

    顺序与 :meth:`ClientService.wait_platform` 逐条对齐，避免同一个平台
    状态在两条路径上得到相反结论。

    Note:
        * 不可恢复的费用不足（``预算 - 已消费``）排在最前面，因为账户预算
          耗尽时平台同样把 ``can_submit`` 置为 ``False``，先看 ``can_submit``
          会白等下去。
        * ``account_remaining_units`` 还要扣在途预留，它不足只是暂时的资金
          占用，不能当成花不起钱；窗口 token 额度同理。
    """
    if not coordinated:
        return Decision("submit", "feedback_disabled")
    if stale_feedback(feedback, now=now):
        return Decision("wait", "stale_feedback", feedback.retry_after_seconds)
    request = request_decision(feedback, reserve_units=reserve_units)
    if request.action == "refuse":
        return request
    if in_flight >= max_inflight:
        return Decision("wait", "local_backpressure", feedback.retry_after_seconds or 0.5)
    if not feedback.can_submit:
        return Decision("wait", "platform_blocked", feedback.retry_after_seconds)
    if request.action == "wait":
        return request
    pending_limit = max(1, feedback.recommended_pending_limit)
    if has_queued and feedback.account_queued >= pending_limit:
        return Decision("wait", "platform_queued_full", feedback.retry_after_seconds)
    return Decision("submit", "platform_open")


def stale_feedback(feedback: PlatformFeedback, *, now: Callable[[], float]) -> bool:
    """快照是否已经旧到不可信。"""
    return now() - observed_epoch(feedback) > STALE_FEEDBACK_SECONDS


def observed_epoch(feedback: PlatformFeedback) -> float:
    """把反馈里的 ISO 时间戳换成纪元秒。"""
    return max(0.0, datetime.fromisoformat(feedback.observed_at).timestamp())
