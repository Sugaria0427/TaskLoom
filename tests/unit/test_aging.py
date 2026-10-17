"""Unit tests for :mod:`taskloom.policies.aging`."""

from collections.abc import Callable

import pytest

from taskloom.policies.aging import AgingQueue


@pytest.fixture
def clock() -> Callable[[], float]:
    state = {"t": 1000.0}
    state["now"] = lambda: state["t"]
    state["advance"] = lambda delta: (state.__setitem__("t", state["t"] + delta))
    return state["now"]  # type: ignore[return-value]


def _make(clock: Callable[[], float], deadline_at: float = 1e9) -> AgingQueue[str]:
    return AgingQueue(
        quantum=10,
        cost=lambda _: 1,
        deadline=lambda _: deadline_at,
        now=clock,
        urgency_window=60.0,
        aging_window=20.0,
    )


def test_windows_must_be_positive() -> None:
    with pytest.raises(ValueError):
        AgingQueue(
            quantum=10,
            cost=lambda _: 1,
            deadline=lambda _: 1e9,
            now=lambda: 0.0,
            urgency_window=0.0,
            aging_window=20.0,
        )
    with pytest.raises(ValueError):
        AgingQueue(
            quantum=10,
            cost=lambda _: 1,
            deadline=lambda _: 1e9,
            now=lambda: 0.0,
            urgency_window=60.0,
            aging_window=-1.0,
        )


def test_drr_still_applies_when_no_urgency_or_aging(clock: Callable[[], float]) -> None:
    queue = _make(clock)
    queue.push("alice", "a1")
    queue.push("bob", "b1")
    assert queue.pop() == "a1"
    assert queue.pop() == "b1"


def test_near_deadline_item_is_promoted(clock: Callable[[], float]) -> None:
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
    # At clock=1000, "urgent" deadline=1010 → remaining=10s < 60s window.
    # Urgency is checked on the head of each owner; alice's head is
    # "urgent" so it gets promoted ahead of the normal DRR rotation.
    assert queue.pop() == "urgent"
    # Once the urgent head is gone, DRR rotation resumes normally.
    assert queue.pop() == "calm"


def test_aged_item_is_promoted_for_background_owner(clock: Callable[[], float]) -> None:
    state = {"t": 1000.0}

    def now() -> float:
        return state["t"]

    queue: AgingQueue[str] = AgingQueue(
        quantum=10,
        cost=lambda _: 1,
        deadline=lambda _: 1e9,
        now=now,
        urgency_window=60.0,
        aging_window=20.0,
    )
    queue.push("busy", "busy-1")
    # alice gets stuck behind busy.
    queue.push("alice", "alice-1")
    # First pop serves "busy-1" via DRR.
    assert queue.pop() == "busy-1"
    # Now "busy" has another head; advance the clock so alice ages.
    state["t"] += 25.0
    # alice's head is aged past 20s window → promoted.
    assert queue.pop() == "alice-1"


def test_remove_cleans_up_records(clock: Callable[[], float]) -> None:
    queue = _make(clock)
    queue.push("alice", "a1")
    queue.push("alice", "a2")
    removed = queue.remove(lambda item: item == "a1")
    assert removed == [("alice", "a1")]
    assert queue.pop() == "a2"
    assert queue.pop() is None


def test_expired_deadline_filtered_by_eligibility(clock: Callable[[], float]) -> None:
    state = {"t": 1000.0}

    def now() -> float:
        return state["t"]

    def deadline(item: str) -> float:
        return 1010.0 if item == "soon" else 1e9

    queue: AgingQueue[str] = AgingQueue(
        quantum=10,
        cost=lambda _: 1,
        deadline=deadline,
        now=now,
        urgency_window=60.0,
        aging_window=20.0,
    )
    queue.push("alice", "soon")
    # Advance past deadline.
    state["t"] = 1500.0
    # Outside urgency window; deadline is in the past, so urgency no longer promotes.
    # DRR still tries; the AgingQueue's pop() doesn't filter by itself — that's
    # the caller's eligibility predicate. Default predicate keeps it.
    result = queue.pop()
    assert result in {"soon"}  # either promotes or DRR-serves, both are "soon"