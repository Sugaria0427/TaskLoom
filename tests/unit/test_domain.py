import pytest
from pydantic import ValidationError

from taskloom.contracts.models import TaskCreate
from taskloom.domain.model import DomainError, ResourcePool, Resources
from taskloom.policies.fair import FairQueue


def test_reservation_is_atomic_and_release_is_idempotent() -> None:
    pool = ResourcePool(Resources(slots=2, memory_bytes=10))
    assert pool.reserve("heavy", Resources(slots=1, memory_bytes=8))
    assert not pool.reserve("other", Resources(slots=1, memory_bytes=4))
    assert "other" not in pool.allocations
    assert pool.available == Resources(slots=1, memory_bytes=2)
    assert pool.reserve("light", Resources(slots=1, memory_bytes=2))
    assert pool.available == Resources()
    assert pool.release("heavy")
    assert not pool.release("heavy")
    assert pool.available == Resources(slots=1, memory_bytes=8)
    with pytest.raises(DomainError, match="capacity"):
        pool.reserve("oversize", Resources(memory_bytes=11))


def test_root_fanout_does_not_create_extra_round_robin_turns() -> None:
    queue: FairQueue[str] = FairQueue()
    for index in range(5):
        queue.push("root-large", f"branch-{index}")
    queue.push("root-small", "interactive")
    assert queue.pop() == "branch-0"
    assert queue.pop() == "interactive"
    assert queue.pop() == "branch-1"


def test_infeasible_head_can_be_bypassed_and_removed() -> None:
    queue: FairQueue[int] = FairQueue()
    queue.push("root", 10)
    queue.push("root", 2)
    assert queue.pop(lambda value: value <= 3) == 2
    assert queue.remove(lambda _: True) == [10]
    assert len(queue) == 0


@pytest.mark.parametrize("name", ["../escape", "sub/file", "..", r"sub\file", "bad\nname"])
def test_document_names_cannot_escape_task_workspace(name: str) -> None:
    with pytest.raises(ValidationError):
        TaskCreate(documents={name: "data"})
