import pytest

from taskloom.contracts.models import PlatformFeedback


@pytest.fixture
def platform_feedback():
    return PlatformFeedback(
        platform_id="test-platform",
        account_id="alice",
        model="test-model",
        pool_capacity=2,
        pool_inflight=0,
        platform_queued=0,
        platform_remaining_units=1000000,
        account_budget_units=250000,
        account_consumed_units=0,
        account_reserved_units=0,
        account_remaining_units=250000,
        account_inflight=0,
        account_max_inflight=1,
        account_queued=0,
        account_window_tokens_remaining=50000,
        account_admission_headroom_units=250000,
        can_submit=True,
        retry_after_seconds=0.1,
    )
