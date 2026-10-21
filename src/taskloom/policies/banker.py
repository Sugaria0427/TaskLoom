"""Phase-aware banker's algorithm over the WP4 resource vector.

The three preconditions the work package requires — a declared maximum demand
per identity, interceptable requests, and returnable resources — are all met by
this module. It computes a safe sequence on every grant and records a rejection
reason otherwise. ``check_safe=False`` degrades to a greedy first-fit allocator,
which the scheduler can switch to as the baseline for comparison.
"""

from dataclasses import dataclass

from taskloom.domain.model import Resources


@dataclass(frozen=True)
class Rejection:
    identity: str
    demand: Resources
    reason: str


class PhaseBanker:
    """Single-resource-vector banker; the owning scheduler serializes calls."""

    def __init__(self, capacity: Resources, check_safe: bool = True) -> None:
        self.capacity = capacity
        self.check_safe = check_safe
        self.allocation: dict[str, Resources] = {}
        self.maximum: dict[str, Resources] = {}
        self.sequence: list[str] = []
        self.rejections: list[Rejection] = []

    @property
    def available(self) -> Resources:
        used = Resources()
        for demand in self.allocation.values():
            used += demand
        return self.capacity - used

    def declare(self, identity: str, maximum: Resources) -> None:
        if not maximum.fits(self.capacity):
            raise ValueError("Declared maximum exceeds resource capacity")
        if not self.allocation.get(identity, Resources()).fits(maximum):
            raise ValueError("Declared maximum is below the current allocation")
        self.maximum[identity] = maximum

    def request(self, identity: str, demand: Resources) -> bool:
        if identity not in self.maximum:
            raise ValueError("Undeclared identity cannot request resources")
        current = self.allocation.get(identity, Resources())
        if not (current + demand).fits(self.maximum[identity]):
            self._reject(identity, demand, "exceeds declared maximum")
            return False
        if not demand.fits(self.available):
            self._reject(identity, demand, "resources unavailable")
            return False
        trial = dict(self.allocation)
        trial[identity] = current + demand
        if self.check_safe and self._safe_sequence(trial) is None:
            self._reject(identity, demand, "would break the safe sequence")
            return False
        self.allocation[identity] = current + demand
        self.sequence = self._safe_sequence(self.allocation) or []
        return True

    def release(self, identity: str) -> Resources:
        return self.allocation.pop(identity, Resources())

    def safe(self) -> list[str] | None:
        return self._safe_sequence(self.allocation)

    def _reject(self, identity: str, demand: Resources, reason: str) -> None:
        self.rejections.append(Rejection(identity, demand, reason))
        if len(self.rejections) > 128:
            self.rejections.pop(0)

    def _safe_sequence(self, allocation: dict[str, Resources]) -> list[str] | None:
        available = self.capacity
        for demand in allocation.values():
            available -= demand
        remaining = set(self.maximum)
        sequence: list[str] = []
        while remaining:
            progressed = False
            for identity in list(remaining):
                need = self.maximum[identity] - allocation.get(identity, Resources())
                if need.fits(available):
                    available += allocation.get(identity, Resources())
                    sequence.append(identity)
                    remaining.discard(identity)
                    progressed = True
            if not progressed:
                return None
        return sequence
