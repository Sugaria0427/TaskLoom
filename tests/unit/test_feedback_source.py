"""Unit tests for :mod:`taskloom.client.feedback_source`."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest

from taskloom.client.feedback_source import FeedbackSource
from taskloom.contracts.models import PlatformFeedback


def _feedback(**overrides: object) -> PlatformFeedback:
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
    )
    base.update(overrides)
    return PlatformFeedback(**base)  # type: ignore[arg-type]


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _reader(
    clock: _Clock, results: list[PlatformFeedback | Exception]
) -> tuple[Callable[[], Awaitable[PlatformFeedback]], list[int]]:
    reads: list[int] = []

    async def read() -> PlatformFeedback:
        reads.append(len(reads))
        outcome = results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return read, reads


def test_interval_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        FeedbackSource(lambda: _feedback(), 0.0)  # type: ignore[arg-type,arg-type]


async def test_snapshot_is_reused_inside_the_interval() -> None:
    clock = _Clock()
    first = _feedback(account_id="first")
    read, reads = _reader(clock, [first, _feedback(account_id="second")])
    source = FeedbackSource(read, 1.0, now=clock)

    assert await source() is first
    clock.advance(0.5)
    assert await source() is first
    assert len(reads) == 1

    clock.advance(0.6)
    assert (await source()).account_id == "second"
    assert len(reads) == 2


async def test_observed_at_is_not_rewritten() -> None:
    """复用快照不能把观测时刻改成读取时刻，否则陈旧判定会永远认为它新。"""
    clock = _Clock()
    stamp = (datetime.now(UTC) - timedelta(seconds=3)).isoformat()
    read, _ = _reader(clock, [_feedback(observed_at=stamp)])
    source = FeedbackSource(read, 10.0, now=clock)

    assert (await source()).observed_at == stamp
    clock.advance(5.0)
    assert (await source()).observed_at == stamp


async def test_read_failure_propagates_instead_of_serving_a_stale_snapshot() -> None:
    clock = _Clock()
    read, _ = _reader(clock, [_feedback(), ConnectionError("platform down")])
    source = FeedbackSource(read, 1.0, now=clock)

    await source()
    clock.advance(2.0)
    with pytest.raises(ConnectionError):
        await source()
    # 失败不该被记成一次成功读取，下一次仍然去平台问。
    assert source.cached is None


async def test_invalidate_forces_the_next_read() -> None:
    clock = _Clock()
    read, reads = _reader(clock, [_feedback(account_id="first"), _feedback(account_id="second")])
    source = FeedbackSource(read, 60.0, now=clock)

    assert (await source()).account_id == "first"
    assert source.cached is not None
    source.invalidate()
    assert source.cached is None
    assert (await source()).account_id == "second"
    assert len(reads) == 2
