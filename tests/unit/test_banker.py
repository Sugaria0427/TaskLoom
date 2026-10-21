import pytest

from taskloom.domain.model import Resources
from taskloom.policies.banker import PhaseBanker


def test_safe_sequence_is_complete_and_starts_with_satisfiable_need() -> None:
    banker = PhaseBanker(Resources(slots=1, memory_bytes=10))
    banker.declare("a", Resources(slots=1, memory_bytes=6))
    banker.declare("b", Resources(slots=1, memory_bytes=6))
    banker.declare("c", Resources(slots=1, memory_bytes=10))
    assert banker.request("a", Resources(slots=1, memory_bytes=6))
    sequence = banker.safe()
    assert sequence is not None
    assert sequence[0] == "a"
    assert set(sequence) == {"a", "b", "c"}


def test_unsafe_allocation_is_refused_with_reason() -> None:
    banker = PhaseBanker(Resources(slots=3, memory_bytes=10))
    for identity in ("a", "b", "c"):
        banker.declare(identity, Resources(slots=1, memory_bytes=10))
    assert banker.request("a", Resources(slots=1, memory_bytes=4))
    assert banker.request("b", Resources(slots=1, memory_bytes=4)) is False
    assert banker.rejections[-1].reason == "would break the safe sequence"


def test_exceeding_declared_maximum_is_refused() -> None:
    banker = PhaseBanker(Resources(slots=1, memory_bytes=10))
    banker.declare("a", Resources(slots=1, memory_bytes=4))
    assert banker.request("a", Resources(slots=1, memory_bytes=6)) is False
    assert banker.rejections[-1].reason == "exceeds declared maximum"


def test_greedy_mode_grants_without_safety_check() -> None:
    greedy = PhaseBanker(Resources(slots=3, memory_bytes=10), check_safe=False)
    for identity in ("a", "b", "c"):
        greedy.declare(identity, Resources(slots=1, memory_bytes=10))
    assert greedy.request("a", Resources(slots=1, memory_bytes=4))
    assert greedy.request("b", Resources(slots=1, memory_bytes=4))
    assert greedy.safe() is None  # deadlocked, yet greedy granted it


def test_release_returns_resources_and_enables_progress() -> None:
    banker = PhaseBanker(Resources(slots=1, memory_bytes=10))
    banker.declare("a", Resources(slots=1, memory_bytes=6))
    banker.declare("b", Resources(slots=1, memory_bytes=6))
    assert banker.request("a", Resources(slots=1, memory_bytes=6))
    assert banker.request("b", Resources(slots=1, memory_bytes=6)) is False
    assert banker.release("a") == Resources(slots=1, memory_bytes=6)
    assert banker.request("b", Resources(slots=1, memory_bytes=6))


def test_declare_maximum_cannot_exceed_capacity() -> None:
    banker = PhaseBanker(Resources(slots=1, memory_bytes=10))
    with pytest.raises(ValueError, match="capacity"):
        banker.declare("a", Resources(slots=1, memory_bytes=11))


def test_undeclared_identity_is_rejected() -> None:
    banker = PhaseBanker(Resources(slots=1, memory_bytes=10))
    with pytest.raises(ValueError, match="Undeclared"):
        banker.request("ghost", Resources(slots=1, memory_bytes=1))
