"""Unit tests for :mod:`taskloom.policies.cost_drr`."""

import pytest

from taskloom.policies.cost_drr import CostDRR


def test_quantum_must_be_positive() -> None:
    with pytest.raises(ValueError):
        CostDRR(quantum=0, cost=lambda _: 1)


def test_one_round_serves_every_owner_its_quantum_worth() -> None:
    queue: CostDRR[str] = CostDRR(quantum=10, cost=lambda _: 1)
    queue.push("alice", "a1")
    queue.push("alice", "a2")
    queue.push("bob", "b1")
    assert sorted(queue.pop()) == ["a1", "a2", "b1"]
    assert queue.pop() == []
    assert queue.blocked() is False


def test_leftover_credit_drains_the_backlog_in_one_round() -> None:
    queue: CostDRR[str] = CostDRR(quantum=10, cost=lambda _: 1)
    for index in range(4):
        queue.push("alice", f"a{index}")
    # All four cost 1 each, so one visit at quantum 10 clears the lot.
    assert queue.pop() == ["a0", "a1", "a2", "a3"]


def test_expensive_item_waits_for_enough_credit() -> None:
    queue: CostDRR[str] = CostDRR(quantum=100, cost=len)
    queue.push("alice", "x" * 250)
    queue.push("alice", "q")
    queue.push("bob", "y")
    # Alice needs three rounds to afford 250 at quantum 100. Bob pays out
    # in the meantime, and when Alice is served it is the expensive head:
    # the cheap tail behind it is never jumped over.
    assert queue.pop() == ["y"]
    assert queue.pop() == []
    assert queue.deficit("alice") == 200
    assert queue.pop() == ["x" * 250, "q"]


def test_cumulative_cost_share_is_fair_across_heterogeneous_loads() -> None:
    """Cost, not request count, decides the share.

    Regression guard for the review finding that a 1000-unit request was
    served as often as a 10-unit one. Both roots are held at a constant
    backlog depth so the numbers are a steady-state share rather than a
    draining queue.
    """
    queue: CostDRR[tuple[str, int]] = CostDRR(quantum=100, cost=lambda item: item[1])
    price_of = {"heavy": 1000, "light": 10}
    depth = 2000
    queued = {"heavy": 0, "light": 0}

    def refill() -> None:
        for owner, price in price_of.items():
            while queued[owner] < depth:
                queue.push(owner, (owner, price))
                queued[owner] += 1

    refill()
    units = dict.fromkeys(price_of, 0)
    counts = dict.fromkeys(price_of, 0)
    for _ in range(3000):
        for owner, price in queue.pop():
            counts[owner] += 1
            units[owner] += price
            queued[owner] -= 1
        refill()

    assert units["heavy"] > 0 and units["light"] > 0
    # Counts must be wildly lopsided: that is the whole point.
    assert counts["light"] > counts["heavy"] * 5
    # Units actually served must land close to even.
    assert abs(units["heavy"] - units["light"]) < 0.05 * max(units["heavy"], units["light"])


def test_eligible_skips_ineligible_heads() -> None:
    queue: CostDRR[int] = CostDRR(quantum=10, cost=lambda _: 1)
    queue.push("alice", 1)
    queue.push("alice", 2)
    queue.push("bob", 10)
    queue.push("bob", 11)
    assert sorted(queue.pop(lambda v: v < 5)) == [1, 2]
    assert queue.pop(lambda v: v < 5) == []
    assert queue.blocked() is True


def test_peek_does_not_remove() -> None:
    queue: CostDRR[str] = CostDRR(quantum=10, cost=lambda _: 1)
    queue.push("alice", "a1")
    assert queue.peek("alice") == "a1"
    assert queue.peek("alice") == "a1"
    assert queue.pop() == ["a1"]
    assert queue.peek("alice") is None
    assert "alice" not in queue


def test_take_charges_the_owner() -> None:
    queue: CostDRR[str] = CostDRR(quantum=10, cost=len)
    queue.push("alice", "abcd")
    queue.push("alice", "z" * 12)
    queue.push("bob", "b")
    assert queue.take("alice") == "abcd"
    # A promotion spends real budget: alice is four into debt, and the
    # debt survives because she still has queued work.
    assert queue.deficit("alice") == -4
    assert len(queue) == 2
    # Her next visit tops the deficit up to 6, still short of the 12-unit
    # head, so bob is served instead of alice being served early.
    assert queue.pop() == ["b"]
    assert queue.deficit("alice") == 6


def test_charge_rejects_negative_amount() -> None:
    queue: CostDRR[str] = CostDRR(quantum=10, cost=lambda _: 1)
    with pytest.raises(ValueError):
        queue.charge("alice", -1)


def test_refund_returns_credit_to_a_queued_owner() -> None:
    queue: CostDRR[str] = CostDRR(quantum=10, cost=len)
    # The 15-unit head is too expensive for one quantum, so alice stays on
    # the ring with a readable deficit.
    queue.push("alice", "x" * 15)
    assert queue.pop() == []
    assert queue.deficit("alice") == 10
    queue.refund("alice", 3)
    assert queue.deficit("alice") == 13


def test_refund_with_a_negative_amount_charges_the_owner() -> None:
    queue: CostDRR[str] = CostDRR(quantum=10, cost=len)
    queue.push("alice", "x" * 15)
    assert queue.pop() == []
    queue.refund("alice", -4)
    assert queue.deficit("alice") == 6


def test_refund_carries_over_for_an_owner_that_left_the_ring() -> None:
    """发完最后一条的一方会退场，结转必须留下并在它回来时并入。"""
    queue: CostDRR[str] = CostDRR(quantum=10, cost=len)
    queue.push("alice", "abcdef")
    assert queue.pop() == ["abcdef"]
    assert "alice" not in queue
    queue.refund("alice", 5)
    assert queue.deficit("alice") == 0
    # Without the carried 5 the 13-unit head would need two rounds; with it
    # the first round tops the deficit to 15 and clears the head.
    queue.push("alice", "x" * 13)
    queue.push("alice", "y")
    assert queue.pop() == ["x" * 13, "y"]


def test_refund_on_an_unknown_owner_is_carried_into_its_next_push() -> None:
    """没在环里的一方先记结转，回来时并入初始额度。"""
    queue: CostDRR[str] = CostDRR(quantum=10, cost=len)
    queue.refund("ghost", 5)
    assert queue.deficit("ghost") == 0
    queue.push("ghost", "x" * 13)
    assert queue.pop() == ["x" * 13]
