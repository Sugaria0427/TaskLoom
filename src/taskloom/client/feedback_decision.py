"""Decide whether L3 should submit, wait, or refuse a model request.

The decision takes the platform feedback snapshot plus a few L3-local
facts (current in-flight count, whether the account already has a
queued request, the deadline of the candidate request) and returns a
:class:`Decision`. Callers translate the decision into the right
``asyncio`` primitive: submit immediately, sleep for ``retry_after``
seconds, or raise a refusal with ``reason`` as the user-facing code.

The decision never reaches out to the platform; it is a pure function
of its arguments. This keeps it deterministic and unit-testable, and
matches the WP3 design where L3 reads feedback but does not negotiate.

Example:
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
    """L3 dispatch decision for a single model request attempt.

    Attributes:
        action: One of ``"submit"``, ``"wait"``, ``"refuse"``.
        reason: Short stable string used as an error code or log key.
            ``"submit"`` always uses ``"platform_open"``.
        retry_after: Seconds the caller should sleep before retrying
            when ``action == "wait"``. Zero otherwise.
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
    """Compute L3's action for one candidate model request.

    Args:
        feedback: Latest platform feedback snapshot for the local
            account. Used as the authoritative source of platform
            admit/queue state.
        now: Callable returning the current epoch seconds. Used to
            judge feedback freshness.
        in_flight: Number of L3 model requests currently in flight on
            this machine.
        max_inflight: Local cap on simultaneous in-flight model
            requests (mirrors ``ClientConfig.max_inflight``).
        coordinated: When ``False``, the local ablation baseline
            always submits; the caller is still responsible for any
            budget check it wants to keep.
        has_queued: ``True`` if the account already has a request
            queued behind the snapshot.
        reserve_units: Cost the candidate request would reserve on the
            account ledger. Compared against
            ``PlatformFeedback.account_remaining_units``.

    Returns:
        A :class:`Decision` whose ``action`` is one of
        ``"submit"``/``"wait"``/``"refuse"``.

    Note:
        预算不足排在 ``can_submit`` 之前。账户预算耗尽时平台把
        ``can_submit`` 也置为 ``False``，若先看它就会把花不起钱误判成
        排队、速率窗口这类可恢复等待，白等下去。费用不足不可恢复，
        必须直接拒绝，与 :meth:`ClientService.wait_platform` 里
        ``ACCOUNT_BUDGET`` 的既有语义一致。
    """
    if not coordinated:
        return Decision("submit", "feedback_disabled")
    age = max(0.0, now() - _observed_epoch(feedback))
    if age > STALE_FEEDBACK_SECONDS:
        return Decision("wait", "stale_feedback", feedback.retry_after_seconds)
    if reserve_units > feedback.account_remaining_units:
        return Decision("refuse", "insufficient_budget")
    if in_flight >= max_inflight:
        return Decision("wait", "local_backpressure", feedback.retry_after_seconds or 0.5)
    if not feedback.can_submit:
        return Decision("wait", "platform_blocked", feedback.retry_after_seconds)
    if has_queued and feedback.recommended_pending_limit <= 0:
        return Decision("wait", "platform_queued_full", feedback.retry_after_seconds)
    return Decision("submit", "platform_open")


def _observed_epoch(feedback: PlatformFeedback) -> float:
    """Convert the feedback's ISO timestamp into epoch seconds."""
    from datetime import datetime

    parsed = datetime.fromisoformat(feedback.observed_at)
    return parsed.timestamp()
