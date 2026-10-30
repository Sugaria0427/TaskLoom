import pytest

from taskloom.domain.model import DomainError, Resources
from taskloom.policies.resources import PhaseResourcePool


def memory(size):
    return Resources(slots=1, memory_bytes=size)


def test_trial_refuses_unsafe_expansion_without_changing_the_single_ledger():
    pool = PhaseResourcePool(Resources(slots=2, memory_bytes=10))
    for key in ("a", "b"):
        pool.declare(key, memory(10))
    assert pool.reserve("a", memory(4))
    assert pool.decision("b", memory(4)) == (False, "would break the safe sequence")
    assert not pool.reserve("b", memory(4))
    assert pool.allocations == {"a": memory(4)}
    assert pool.grant("a", memory(10))
    assert pool.grant("a", memory(10))  # replay of the absolute target
    assert pool.available == Resources(slots=1)
    assert pool.release("a")
    assert not pool.release("a")
    assert "a" not in pool.maximum
    assert pool.reserve("b", memory(4))


def test_dynamic_maximum_update_is_atomic_and_never_below_held_amount():
    pool = PhaseResourcePool(Resources(slots=3))
    pool.declare("a", Resources(slots=3))
    pool.declare("b", Resources(slots=2))
    pool.reserve("a", Resources(slots=1))
    pool.reserve("b", Resources(slots=1))
    before = pool.maximum.copy(), pool.allocations.copy()
    with pytest.raises(DomainError, match="safe sequence"):
        pool.declare("b", Resources(slots=3))
    assert (pool.maximum, pool.allocations) == before
    with pytest.raises(DomainError, match="below current"):
        pool.declare("b", Resources())
    pool.declare("a", Resources(slots=2))
    assert pool.snapshot()["safe_sequence"] is not None


def test_restore_keeps_unknown_occupancy_even_when_no_safe_sequence_exists():
    pool = PhaseResourcePool(Resources(slots=2, memory_bytes=10))
    pool.restore("a", memory(4), memory(10))
    pool.restore("b", memory(4), memory(10))
    assert pool.snapshot()["safe_sequence"] is None
    assert pool.reserve("a", memory(4))  # reconcile original identity
    assert not pool.grant("b", memory(5))
    assert pool.release("a")
    assert pool.grant("b", memory(10))
    with pytest.raises(DomainError, match="capacity"):
        pool.restore("c", memory(1), memory(11))


def test_greedy_baseline_keeps_capacity_and_artifacts_outside_banker_proof():
    pool = PhaseResourcePool(Resources(slots=2, memory_bytes=10), check_safe=False)
    for key in ("a", "b"):
        pool.declare(key, memory(10))
        assert pool.reserve(key, memory(4))
    assert pool.snapshot()["safe_sequence"] is None
    assert not pool.grant("a", memory(10))
    assert pool.snapshot()["proof_dimensions"] == ["slots", "memory_bytes"]


def test_cancelled_declarations_are_removed_and_multi_resource_admission_is_atomic():
    pool = PhaseResourcePool(Resources(slots=2, memory_bytes=10, artifact_bytes=10))
    pool.declare("waiting", memory(10))
    pool.forget("waiting")
    assert not pool.maximum
    assert pool.reserve("a", Resources(slots=1, memory_bytes=1, artifact_bytes=8))
    assert not pool.reserve("b", Resources(slots=1, memory_bytes=1, artifact_bytes=3))
    assert "b" not in pool.allocations


def test_zero_allocation_declaration_is_accepted_and_admission_still_waits_for_safety():
    pool = PhaseResourcePool(Resources(slots=3, memory_bytes=10))
    pool.restore("a", memory(4), memory(10))
    pool.restore("b", memory(4), memory(10))
    pool.declare("fresh", memory(2))  # used to raise RESOURCE_UNSAFE forever
    assert pool.decision("fresh", memory(1)) == (False, "would break the safe sequence")
    assert not pool.grant("fresh", memory(1))
    with pytest.raises(DomainError, match="safe sequence"):
        pool.declare("b", memory(9))  # still checked: b holds resources
    assert pool.release("a")
    assert pool.grant("fresh", memory(1))
