"""Unit tests for :mod:`taskloom.policies.cost_drr`."""

import pytest

from taskloom.policies.cost_drr import CostDRR


def test_quantum_must_be_positive() -> None:
    with pytest.raises(ValueError):
        CostDRR(quantum=0, cost=lambda _: 1)


def test_owners_rotate_under_equal_cost() -> None:
    queue: CostDRR[str] = CostDRR(quantum=10, cost=lambda _: 1)
    queue.push("alice", "a1")
    queue.push("bob", "b1")
    queue.push("alice", "a2")
    queue.push("bob", "b2")
    assert queue.pop() == "a1"
    assert queue.pop() == "b1"
    assert queue.pop() == "a2"
    assert queue.pop() == "b2"
    assert queue.pop() is None


def test_long_request_does_not_starve_short_owner() -> None:
    queue: CostDRR[str] = CostDRR(quantum=100, cost=lambda item: len(item))
    queue.push("alice", "xxxx")  # cost 4
    queue.push("alice", "yyyyyyyyyyyy")  # cost 12
    queue.push("bob", "z")  # cost 1
    queue.push("bob", "w")  # cost 1
    # Classical DRR: one item per visit, rotate, credit carries. Alice
    # gets xxxx (4) and the long item across her two visits; bob's z
    # and w are served on his visits without being delayed by alice's
    # bigger backlog.
    assert [queue.pop() for _ in range(4)] == [
        "xxxx",
        "z",
        "yyyyyyyyyyyy",
        "w",
    ]
    assert queue.pop() is None


def test_credit_carries_over_for_small_items() -> None:
    queue: CostDRR[str] = CostDRR(quantum=10, cost=lambda _: 2)
    queue.push("alice", "a1")
    queue.push("alice", "a2")
    queue.push("alice", "a3")
    queue.push("alice", "a4")
    queue.push("alice", "a5")
    # First turn has quantum=10 → serves 5 items at cost 2 each.
    assert [queue.pop() for _ in range(5)] == ["a1", "a2", "a3", "a4", "a5"]
    assert queue.remaining("alice") == 0


def test_eligible_skips_ineligible_heads() -> None:
    queue: CostDRR[int] = CostDRR(quantum=10, cost=lambda _: 1)
    queue.push("alice", 1)
    queue.push("alice", 2)
    queue.push("bob", 10)
    queue.push("bob", 11)
    assert queue.pop(lambda v: v < 5) == 1
    assert queue.pop(lambda v: v < 5) == 2
    # Alice drained; bob's heads remain.
    assert queue.pop(lambda v: v < 5) is None


def test_peek_does_not_remove() -> None:
    queue: CostDRR[str] = CostDRR(quantum=10, cost=lambda _: 1)
    queue.push("alice", "a1")
    assert queue.peek("alice") == "a1"
    assert queue.peek("alice") == "a1"
    assert queue.pop() == "a1"
    assert queue.peek("alice") is None
    assert "alice" not in queue


def test_take_removes_specific_owner_head() -> None:
    queue: CostDRR[str] = CostDRR(quantum=10, cost=lambda _: 1)
    queue.push("alice", "a1")
    queue.push("bob", "b1")
    assert queue.take("alice") == "a1"
    assert len(queue) == 1
    # Bob is now the sole owner and pops next.
    assert queue.pop() == "b1"


def test_remove_returns_owner_item_pairs() -> None:
    queue: CostDRR[str] = CostDRR(quantum=10, cost=lambda _: 1)
    queue.push("alice", "a1")
    queue.push("alice", "a2")
    queue.push("bob", "b1")
    removed = queue.remove(lambda item: item in {"a1", "b1"})
    assert sorted(removed) == [("alice", "a1"), ("bob", "b1")]
    assert queue.pop() == "a2"
    assert queue.pop() is None


def test_remaining_credit_is_bounded_by_quantum() -> None:
    queue: CostDRR[str] = CostDRR(quantum=5, cost=lambda _: 1)
    queue.push("alice", "a1")
    queue.push("alice", "a2")
    queue.push("alice", "a3")
    queue.push("alice", "a4")
    queue.push("alice", "a5")
    queue.push("alice", "a6")
    queue.pop()
    queue.pop()
    queue.pop()
    queue.pop()
    queue.pop()
    # Credit must never exceed quantum.
    assert queue.remaining("alice") <= 5
    assert queue.pop() == "a6"