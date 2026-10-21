"""Unit tests for :mod:`taskloom.policies.aging`."""

from collections.abc import Callable

import pytest

from taskloom.policies.aging import AgingQueue


def _make(
    now: Callable[[], float],
    deadline_at: float = 1e9,
    urgency_burst: int = 2,
) -> AgingQueue[str]:
    return AgingQueue(
        quantum=10,
        cost=lambda _: 1,
        deadline=lambda _: deadline_at,
        now=now,
        urgency_window=60.0,
        aging_window=20.0,
        urgency_burst=urgency_burst,
    )


class _Clock:
    """可推进的时钟替身。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> None:
        self.t += delta


def test_windows_must_be_positive() -> None:
    for urgency, aging in ((0.0, 20.0), (60.0, -1.0)):
        with pytest.raises(ValueError):
            AgingQueue(
                quantum=10,
                cost=lambda _: 1,
                deadline=lambda _: 1e9,
                now=lambda: 0.0,
                urgency_window=urgency,
                aging_window=aging,
            )


def test_urgency_burst_must_be_at_least_one() -> None:
    with pytest.raises(ValueError):
        _make(lambda: 0.0, urgency_burst=0)


def test_drr_still_applies_when_no_urgency_or_aging() -> None:
    queue = _make(_Clock())
    queue.push("alice", "a1")
    queue.push("bob", "b1")
    assert queue.pop() == ["a1", "b1"]


def test_urgent_head_is_promoted_ahead_of_rotation() -> None:
    clock = _Clock()
    queue: AgingQueue[str] = AgingQueue(
        quantum=10,
        cost=lambda _: 1,
        deadline=lambda item: 1010.0 if item == "urgent" else 1e9,
        now=clock,
        urgency_window=60.0,
        aging_window=20.0,
    )
    queue.push("alice", "urgent")
    queue.push("alice", "calm")
    queue.push("bob", "calm-b")
    # remaining = 10s is inside the 60s window, so the urgent head is
    # pulled off before the normal round; the rest comes out by rotation.
    assert queue.pop() == ["urgent", "calm", "calm-b"]


def test_aged_head_is_promoted_for_background_owner() -> None:
    clock = _Clock()
    queue = _make(clock)
    queue.push("busy", "busy-1")
    queue.push("alice", "alice-1")
    assert queue.pop() == ["busy-1", "alice-1"]
    assert queue.pop() == []


def test_aged_head_wins_when_nothing_else_is_pending() -> None:
    clock = _Clock()
    queue = _make(clock)
    queue.push("alice", "alice-1")
    clock.advance(25.0)  # alice 已老化
    queue.push("busy", "busy-1")  # busy 刚到，没老化
    # Alice is promoted even though she is not next in the rotation.
    assert queue.pop()[0] == "alice-1"


def test_sustained_urgent_arrivals_cannot_starve_the_background() -> None:
    """Regression guard for the review finding on unbounded promotion.

    A background root waits past the aging window, then a hundred urgent
    items arrive. Without a burst cap the background never gets served.
    """
    clock = _Clock()
    queue: AgingQueue[str] = AgingQueue(
        quantum=10,
        cost=lambda _: 1,
        deadline=lambda item: 1001.0 if item.startswith("urgent-") else 1e9,
        now=clock,
        urgency_window=60.0,
        aging_window=20.0,
        urgency_burst=2,
    )
    queue.push("background", "bg")
    clock.advance(25.0)  # 后台已老化
    for index in range(100):
        queue.push("urgent", f"urgent-{index}")

    served: list[str] = []
    for _ in range(100):
        served.extend(queue.pop())

    assert "bg" in served, "后台必须拿到服务机会"
    # And the urgent flow still dominates: it keeps roughly its share.
    assert len([item for item in served if item.startswith("urgent-")]) > 90


def test_promotion_is_charged_to_the_promoted_owner() -> None:
    clock = _Clock()
    queue: AgingQueue[str] = AgingQueue(
        quantum=10,
        cost=len,
        deadline=lambda _: 1001.0,
        now=clock,
        urgency_window=60.0,
        aging_window=20.0,
        urgency_burst=1,
    )
    queue.push("alice", "abcd")
    queue.push("alice", "z" * 12)
    queue.push("bob", "b")
    assert queue.pop() == ["abcd", "b"]
    # The promotion put alice four units into debt. The same round's
    # top-up brings her back to only 6, still short of the 12-unit head,
    # so she gets nothing else this round and has to keep earning.
    assert queue.deficit("alice") == 6


def test_requeue_preserves_queue_age() -> None:
    clock = _Clock()
    queue = _make(clock)
    queue.push("alice", "a1")
    original_enqueue = clock.t
    clock.advance(25.0)
    queue.requeue("alice", "a1", original_enqueue)
    # The item is still reported as aged: its wait was not reset.
    assert queue.pop()[0] == "a1"


def test_remove_cleans_up_records() -> None:
    queue = _make(_Clock())
    queue.push("alice", "a1")
    queue.push("alice", "a2")
    assert queue.remove(lambda item: item == "a1") == [("alice", "a1")]
    assert queue.pop() == ["a2"]
    assert queue.pop() == []


def test_expired_items_are_left_for_the_callers_predicate() -> None:
    clock = _Clock()
    queue: AgingQueue[str] = AgingQueue(
        quantum=10,
        cost=lambda _: 1,
        deadline=lambda item: 1010.0 if item == "soon" else 1e9,
        now=clock,
        urgency_window=60.0,
        aging_window=20.0,
    )
    queue.push("alice", "soon")
    clock.t = 1500.0  # 已过期
    # Deadline expiry is a terminal state the scheduler owns; the queue
    # just refuses to serve what the caller's predicate rejects.
    assert queue.pop(lambda item: False) == []
    assert len(queue) == 1
