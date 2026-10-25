"""Measured facts behind every calibrated number in the benchmark, with their provenance.

FACTS holds the values the generators use, so the benchmark runs without private traces.
`measure` recomputes them from the local CSVs; test_bench checks the two agree, which keeps
each constant traceable to data rather than to a guess.

Sources: Aether `usage` (2026-07-06..10-19, 22 users, 34 keys, 260,881 rows; 71 upstream
keys of 8 providers) and abyss
captures (183 Claude Code / Codex sessions, 12,862 requests). Prices are public list-price
ratios because Aether records zero cost for this deployment.
"""

import math
from collections import Counter, defaultdict
from dataclasses import dataclass

from .traces import TraceRequest

SPLIT_GAP = 600.0  # a chain idle this long starts a new root task (the next human turn)
# Past five minutes of waiting a chain's prompt cache starts to fade (CACHE_HITS) and an
# interactive user has long noticed; the benchmark counts such a wait as starvation.
STARVATION_WAIT = 300.0

# Prompt caches belong to one upstream account. Aether since 2026-09-05, when each provider is
# a separate account: cache hit rate (cached / input tokens, requests >= 20k tokens) by how long
# the same user had left that upstream key idle, start to start. Points sit at the geometric
# middle of the measured buckets (<1m, 1-5m, 5-10m, 10-30m, 30-60m, 1-6h, >6h). The cache fades
# over about an hour instead of ending at a fixed TTL.
CACHE_HITS = (
    (30.0, 0.928), (134.0, 0.913), (424.0, 0.809), (1039.0, 0.697),
    (2546.0, 0.569), (8818.0, 0.310), (43200.0, 0.046),
)  # fmt: skip
# Hit rate on an upstream key the user had not used: only prompts shared with other users.
CACHE_HIT_COLD = 0.116
# First-byte time rises ~2 s from 25k to 200k uncached tokens (bucket medians; per-request
# R^2 ~0), so a cache miss costs money far more than time.
PREFILL_SECONDS_PER_TOKEN = 1e-5


def cache_retention(idle: float | None) -> float:
    """Share of a chain's warm cached prefix still cached on an upstream account.

    `idle` is the time since the account last served the chain, None if it never did.
    """
    warm = CACHE_HITS[0][1]
    if idle is None:
        return CACHE_HIT_COLD / warm
    if idle <= CACHE_HITS[0][0]:
        return 1.0
    for (t0, h0), (t1, h1) in zip(CACHE_HITS, CACHE_HITS[1:], strict=False):
        if idle <= t1:
            share = math.log(idle / t0) / math.log(t1 / t0)
            return (h0 + (h1 - h0) * share) / warm
    return CACHE_HITS[-1][1] / warm


@dataclass(frozen=True)
class Price:
    """Token-equivalents per token, relative to one uncached input token."""

    output: float
    cache_read: float
    cache_write: float


# Anthropic: output 5x input, cache read 0.1x, 5-minute cache write 1.25x.
# OpenAI, as Aether's global_models prices the models in the traces (<= 272k-token tier):
# gpt-6-astra $10/$50/$1 per 1M input/output/cached, gpt-6-sol and gpt-6.1-sol 5x output,
# gpt-5.6 5-6x. OpenAI reports no cache writes, so a missed prefix is billed as fresh input.
PRICES = {"claude": Price(5.0, 0.1, 1.25), "gpt": Price(5.0, 0.1, 1.0)}


def price(model: str) -> Price:
    return PRICES["gpt" if model.startswith("gpt") else "claude"]


FACTS: dict[str, float] = {
    # Aether, completed requests.
    "input_p50": 110_300,
    "input_p90": 216_600,
    "output_p50": 242,
    "output_p90": 1_476,
    "output_p99": 5_306,
    "cached_fraction": 0.919,
    "latency_base_seconds": 6.0,  # 5.1-8.9 s across the three busiest models
    "latency_per_output_token": 0.025,  # 20-30 ms, R^2 0.80-0.94
    "fault_reject": 0.065,  # failed requests, ~96% upstream 5xx before execution
    "fault_lost": 0.002,  # executed, stream ended without a reply
    "fault_lost_forever": 0.001,
    "outage_failure_share": 0.697,  # failures inside minutes where >= half failed
    "outage_minutes_p50": 1,
    "outage_minutes_max": 10,
    "user_peak_concurrency": 16,  # most parallel in-flight requests of one user
    "user_burst_per_minute": 81,  # most requests one user started in one minute
    "top_user_share": 0.358,
    # Aether lanes and abyss sessions split at SPLIT_GAP.
    "think_p50": 5.0,
    "think_p75": 12.0,
    "rounds_p50": 10,
    "rounds_p90": 77,
}

# Request share of Aether's users, largest first (sums to ~1 over 22 users).
USER_SHARES = (
    0.358, 0.163, 0.120, 0.116, 0.072, 0.040, 0.025, 0.018, 0.014, 0.013, 0.012, 0.011,
)  # fmt: skip


def lognormal_from(p50: float, p_hi: float, z_hi: float) -> tuple[float, float]:
    """(mu, sigma) of the lognormal whose median is p50 and whose z_hi quantile is p_hi."""
    return math.log(p50), math.log(p_hi / p50) / z_hi


Z90, Z75, Z99 = 1.2816, 0.6745, 2.3263


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))]


def measure(aether: list[TraceRequest], abyss: list[TraceRequest]) -> dict[str, float]:
    """Recompute the trace-derived FACTS (prices and outage shape excluded)."""
    from .calibrate import lanes, sessions, usable

    done = usable(aether)
    rounds: list[float] = []
    thinks: list[float] = []
    for segment in [*lanes(done), *sessions(usable(abyss))]:
        rounds.append(len(segment.rows))
        thinks.extend(
            max(0.0, b.start - a.end) for a, b in zip(segment.rows, segment.rows[1:], strict=False)
        )
    by_user = Counter(r.user for r in aether)
    events: dict[str, list[tuple[float, int]]] = defaultdict(list)
    for row in done:
        events[row.user] += [(row.start, 1), (row.end, -1)]
    peak = 0
    for user_events in events.values():
        level = 0
        for _, delta in sorted(user_events):
            level += delta
            peak = max(peak, level)
    failed = [r for r in aether if r.status == "failed"]
    return {
        "input_p50": _quantile([r.input_tokens for r in done], 0.5),
        "input_p90": _quantile([r.input_tokens for r in done], 0.9),
        "output_p50": _quantile([r.output_tokens for r in done], 0.5),
        "output_p90": _quantile([r.output_tokens for r in done], 0.9),
        "output_p99": _quantile([r.output_tokens for r in done], 0.99),
        "cached_fraction": sum(r.cached_tokens for r in done) / sum(r.input_tokens for r in done),
        "fault_reject": len(failed) / len(aether),
        "user_peak_concurrency": peak,
        "user_burst_per_minute": max(
            Counter((r.user, int(r.start // 60)) for r in aether).values()
        ),
        "top_user_share": max(by_user.values()) / len(aether),
        "think_p50": _quantile(thinks, 0.5),
        "think_p75": _quantile(thinks, 0.75),
        "rounds_p50": _quantile(rounds, 0.5),
        "rounds_p90": _quantile(rounds, 0.9),
    }
