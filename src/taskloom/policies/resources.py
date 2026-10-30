"""One allocation ledger; banker instances are disposable safety calculations."""

from dataclasses import asdict

from taskloom.domain.model import DomainError, ResourcePool, Resources
from taskloom.policies.banker import PhaseBanker


def reusable(value: Resources) -> Resources:
    # Retained artifacts are not returnable memory and are not a banker proof.
    return Resources(slots=value.slots, memory_bytes=value.memory_bytes)


class PhaseResourcePool(ResourcePool):
    def __init__(self, capacity: Resources, check_safe: bool = True) -> None:
        super().__init__(capacity)
        self.check_safe = check_safe
        self.maximum: dict[str, Resources] = {}
        self.blocked: dict[str, str] = {}
        self.history: list[dict[str, object]] = []

    def _record(self, identity: str, target: Resources, reason: str, granted: bool) -> None:
        self.history.append(
            {"identity": identity, "target": asdict(target), "reason": reason, "granted": granted}
        )
        self.history = self.history[-128:]

    def record_blocked(self, identity: str, target: Resources, reason: str) -> None:
        if self.blocked.get(identity) != reason:
            self._record(identity, target, reason, False)
        self.blocked[identity] = reason

    def _banker(
        self, allocations: dict[str, Resources], maxima: dict[str, Resources]
    ) -> PhaseBanker:
        banker = PhaseBanker(reusable(self.capacity), self.check_safe)
        banker.allocation = {key: reusable(value) for key, value in allocations.items()}
        banker.maximum = {key: reusable(value) for key, value in maxima.items()}
        return banker

    def declare(self, identity: str, maximum: Resources) -> None:
        if self.maximum.get(identity) == maximum:
            return
        if not maximum.fits(self.capacity):
            raise DomainError("RESOURCE_TOO_LARGE", "Maximum exceeds local capacity")
        if not self.allocations.get(identity, Resources()).fits(maximum):
            raise DomainError("RESOURCE_MAXIMUM_CONFLICT", "Maximum is below current allocation")
        trial = {**self.maximum, identity: maximum}
        # A claimant holding nothing can always finish last because its maximum fits
        # the capacity, so its declaration cannot change safety. Skipping the proof
        # lets new work queue behind restored unsafe occupancy; admission still checks.
        if (
            self.check_safe
            and identity in self.allocations
            and self._banker(self.allocations, trial).safe() is None
        ):
            raise DomainError("RESOURCE_UNSAFE", "Maximum update would break the safe sequence")
        self.maximum = trial

    def decision(self, identity: str, target: Resources) -> tuple[bool, str | None]:
        maximum = self.maximum.get(identity, target)
        current = self.allocations.get(identity, Resources())
        if identity in self.allocations and current == target:
            return True, None
        if not current.fits(target) or not target.fits(maximum):
            return False, "exceeds declared maximum or decreases a held allocation"
        delta = target - current
        if not delta.fits(self.available):
            return False, "resources unavailable"
        trial = {**self.allocations, identity: target}
        maxima = {**self.maximum, identity: maximum}
        if self.check_safe and self._banker(trial, maxima).safe() is None:
            return False, "would break the safe sequence"
        return True, None

    def reserve(self, identity: str, demand: Resources) -> bool:
        if identity in self.allocations:
            if self.allocations[identity] != demand:
                raise DomainError("RESERVATION_CONFLICT", "Identity has a different demand")
            return True
        if identity not in self.maximum:
            self.declare(identity, demand)
        return self.grant(identity, demand)

    def grant(self, identity: str, target: Resources) -> bool:
        """Absolute targets make retries idempotent; callers serialize and persist."""
        if identity not in self.maximum:
            raise DomainError("RESOURCE_UNDECLARED", "Declare maximum before requesting")
        allowed, reason = self.decision(identity, target)
        if not allowed:
            self.record_blocked(identity, target, reason or "resource admission denied")
            return False
        if self.allocations.get(identity) != target:
            self._record(
                identity, target, "safe grant" if self.check_safe else "capacity grant", True
            )
        self.allocations[identity] = target
        self.blocked.pop(identity, None)
        return True

    def restore(self, identity: str, allocation: Resources, maximum: Resources) -> None:
        """Restore facts, including unknown executions, before admitting new work."""
        if not allocation.fits(maximum) or not maximum.fits(self.capacity):
            raise DomainError(
                "RESOURCE_RESTORE_CONFLICT", "Saved claim exceeds configured capacity"
            )
        if identity in self.allocations:
            if self.allocations[identity] != allocation or self.maximum[identity] != maximum:
                raise DomainError("RESOURCE_RESTORE_CONFLICT", "Saved identity has another claim")
            return
        if not allocation.fits(self.available):
            raise DomainError("RESOURCE_RESTORE_CONFLICT", "Saved occupancy exceeds capacity")
        self.allocations[identity] = allocation
        self.maximum[identity] = maximum

    def forget(self, identity: str) -> None:
        if identity not in self.allocations:
            self.maximum.pop(identity, None)
            self.blocked.pop(identity, None)

    def release(self, identity: str) -> bool:
        released = super().release(identity)
        self.forget(identity)
        return released

    def snapshot(self) -> dict[str, object]:
        return {
            "enabled": self.check_safe,
            "proof_dimensions": ["slots", "memory_bytes"],
            "safe_sequence": self._banker(self.allocations, self.maximum).safe(),
            "maximum": {key: asdict(value) for key, value in self.maximum.items()},
            "blocked": dict(self.blocked),
            "history": list(self.history),
        }
