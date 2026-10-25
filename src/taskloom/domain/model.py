from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


@dataclass(frozen=True)
class Agent:
    agent_id: str
    root_task_id: str
    parent_agent_id: str | None = None


@dataclass(frozen=True)
class Step:
    step_id: str
    root_task_id: str
    agent_id: str
    kind: Literal["model", "tool"]
    input_version: str
    predecessors: tuple[str, ...]
    current_attempt_id: str


class Outcome(StrEnum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class WorkState(StrEnum):
    READY = "READY"
    WAIT_DEPENDENCY = "WAIT_DEPENDENCY"
    WAIT_RESOURCE = "WAIT_RESOURCE"
    WAIT_QUOTA = "WAIT_QUOTA"
    RUNNING = "RUNNING"
    RECONCILING = "RECONCILING"
    FINISHED = "FINISHED"


class DomainError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class Resources:
    slots: int = 0
    memory_bytes: int = 0
    artifact_bytes: int = 0

    def __post_init__(self) -> None:
        if min(self.slots, self.memory_bytes, self.artifact_bytes) < 0:
            raise ValueError("Resource quantities must be nonnegative")

    def fits(self, other: "Resources") -> bool:
        return (
            self.slots <= other.slots
            and self.memory_bytes <= other.memory_bytes
            and self.artifact_bytes <= other.artifact_bytes
        )

    def __add__(self, other: "Resources") -> "Resources":
        return Resources(
            self.slots + other.slots,
            self.memory_bytes + other.memory_bytes,
            self.artifact_bytes + other.artifact_bytes,
        )

    def __sub__(self, other: "Resources") -> "Resources":
        return Resources(
            self.slots - other.slots,
            self.memory_bytes - other.memory_bytes,
            self.artifact_bytes - other.artifact_bytes,
        )


class ResourcePool:
    """Pure reservation rules; its owner serializes changes."""

    def __init__(self, capacity: Resources) -> None:
        self.capacity = capacity
        self.allocations: dict[str, Resources] = {}

    @property
    def available(self) -> Resources:
        used = Resources()
        for allocation in self.allocations.values():
            used += allocation
        return self.capacity - used

    def reserve(self, identity: str, demand: Resources) -> bool:
        if identity in self.allocations:
            if self.allocations[identity] != demand:
                raise DomainError("RESERVATION_CONFLICT", "Identity has a different demand")
            return True
        if not demand.fits(self.capacity):
            raise DomainError("RESOURCE_TOO_LARGE", "Tool exceeds the resource domain capacity")
        if not demand.fits(self.available):
            return False
        self.allocations[identity] = demand
        return True

    def release(self, identity: str) -> bool:
        return self.allocations.pop(identity, None) is not None

    def shortage(self, demand: Resources) -> Resources:
        """Per-dimension deficit against current availability; zero where satisfied."""
        return Resources(
            slots=max(0, demand.slots - self.available.slots),
            memory_bytes=max(0, demand.memory_bytes - self.available.memory_bytes),
            artifact_bytes=max(0, demand.artifact_bytes - self.available.artifact_bytes),
        )

    def admission(self, demand: Resources) -> tuple[bool, str | None]:
        """Whether ``demand`` can be admitted now, and the blocking reason when it cannot.

        WP3 ranks candidates by feasibility; this method is the single source of
        that fact, returning the reason a demand is blocked rather than a bare
        boolean.
        """
        if not demand.fits(self.capacity):
            return False, "exceeds total capacity"
        shortage = self.shortage(demand)
        if shortage == Resources():
            return True, None
        blockers: list[str] = []
        if shortage.slots:
            blockers.append(f"{shortage.slots} slots")
        if shortage.memory_bytes:
            blockers.append(f"{shortage.memory_bytes} memory bytes")
        if shortage.artifact_bytes:
            blockers.append(f"{shortage.artifact_bytes} artifact bytes")
        return False, "short of " + ", ".join(blockers)
