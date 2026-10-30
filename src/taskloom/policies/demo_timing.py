"""Deterministic display timing; independent of billing and real provider performance."""


def mapped_seconds(
    reference_ms: float,
    strength: float,
    exponent: float,
    amount: float,
    reference_amount: float,
    *,
    workload: bool = True,
    cap_seconds: float = 120,
) -> float:
    baseline = reference_ms / 1000
    if workload:
        baseline = max(0.1, baseline * (max(1, amount) / reference_amount) ** exponent)
    return min(cap_seconds, baseline * strength)
