"""Unit tests for :mod:`taskloom.client.feedback_decision`."""

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
    # observed_at is older than STALE_FEEDBACK_SECONDS; fix now() to a
    # far-future epoch so the diff is well-defined.
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
    feedback = fresh_feedback(account_remaining_units=100)
    decision = decide(
        feedback,
        now=lambda: 0.0,
        in_flight=0,
        max_inflight=1,
        coordinated=True,
        has_queued=False,
        reserve_units=200,
    )
    assert decision.action == "refuse"
    assert decision.reason == "insufficient_budget"


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
