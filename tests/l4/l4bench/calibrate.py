"""Workloads at benchmark scale, from traces or from the measured distributions in facts.

Units. Real prompts run to 10^5 tokens, which the in-process gateway would serialize on every
scan, so every token count is divided by SCALE before it reaches the gateway. Service times
stay in real seconds, so pool occupancy is real. Pool charges are price-weighted
token-equivalents (1 unit = SCALE uncached input tokens): what a request truly costs. The
gateway admits budgets on a worst case and reserves its own estimate of the charge, and the
benchmark measures how far that estimate is from the charge.
"""

import math
import random
from collections import defaultdict
from dataclasses import dataclass, replace

from l4kit import AccountSpec, Chain, Step, TaskSpec, Workload

from .facts import (
    FACTS,
    PREFILL_SECONDS_PER_TOKEN,
    SPLIT_GAP,
    USER_SHARES,
    Z75,
    Z90,
    Price,
    lognormal_from,
    price,
)  # fmt: skip
from .traces import TraceRequest

SCALE = 100
MAX_TOKENS = 32_000  # Claude Code's default output ceiling; traces do not record the request's
SPAN = 3600.0  # seconds of arrivals per general sample
# Every run stays under the gateway's 1024-record lifetime cap, which bounds a sample's ideal
# concurrency to about 2.5-6 slots. Raising it once the cap is fixed changes the samples, so it
# bumps the version and old results stop being comparable. Version 2 splits the pool into
# upstream accounts with separate prompt caches.
VERSION = 3
MAX_REQUESTS = 900

# Prices the benchmark platform admits and estimates with (suites.platform). Charges are already
# SCALE-scaled token-equivalents, so the multipliers apply directly; a missed prompt is billed
# at the larger cache-write price of the two model families.
GATEWAY_PRICES = Price(output=5.0, cache_read=0.1, cache_write=1.25)

INPUT = lognormal_from(FACTS["input_p50"], FACTS["input_p90"], Z90)
OUTPUT = lognormal_from(FACTS["output_p50"], FACTS["output_p90"], Z90)
THINK = lognormal_from(FACTS["think_p50"], FACTS["think_p75"], Z75)
ROUNDS = lognormal_from(FACTS["rounds_p50"], FACTS["rounds_p90"], Z90)


def tokens(real: float) -> int:
    return max(1, round(real / SCALE))


def service_seconds(output: int) -> float:
    return FACTS["latency_base_seconds"] + FACTS["latency_per_output_token"] * output


def make_step(
    prompt: int,
    output: int,
    cached: int = 0,
    written: int = 0,
    service: float | None = None,
    think: float = 0.0,
    model: str = "claude",
) -> Step:
    output = min(output, MAX_TOKENS)
    rates = price(model)
    fresh = max(0, prompt - cached - written)
    charge = fresh + rates.cache_read * cached + rates.cache_write * written + rates.output * output
    return Step(
        # The mock tokenizer counts message framing, so tiny prompts measure ~12 tokens anyway.
        input_tokens=max(16, tokens(prompt)),
        output_tokens=tokens(output),
        cost_units=tokens(charge),
        service_seconds=round(service if service else service_seconds(output), 3),
        think_seconds=round(think, 3),
        # A missed cached token is written again instead of read.
        cached_tokens=round(cached / SCALE),
        cache_discount=rates.cache_write - rates.cache_read,
        cold_seconds=round(cached * PREFILL_SECONDS_PER_TOKEN, 3),
    )


def ideal_seconds(task: TaskSpec) -> float:
    """Completion time on an idle platform: offsets, thinking and service, chain by chain."""

    def chain_time(branch: int) -> float:
        offset = task.chains[branch].offset if task.chains else 0.0
        return offset + sum(
            step.think_seconds + (step.service_seconds or 0.0) for step in task.steps(branch)
        )

    return max(chain_time(b) for b in range(task.chain_count))


def offered_slots(tasks: list[TaskSpec] | tuple[TaskSpec, ...], span: float) -> float:
    served = sum(
        step.service_seconds or 0.0
        for task in tasks
        for b in range(task.chain_count)
        for step in task.steps(b)
    )
    return served / span


def ideal_concurrency(tasks: list[TaskSpec] | tuple[TaskSpec, ...]) -> float:
    """Mean busy slots over the workload's span on a platform with unlimited capacity.

    This is the load definition: capacity = ideal_concurrency / load. It follows each chain's
    own timeline, so work that closed-loop chains do after the last arrival is counted where
    it happens rather than spread over the arrival window.
    """
    busy, first, last = 0.0, float("inf"), 0.0
    for task in tasks:
        for branch in range(task.chain_count):
            clock = task.start_at + (task.chains[branch].offset if task.chains else 0.0)
            first = min(first, clock)
            for step in task.steps(branch):
                clock += step.think_seconds
                served = step.service_seconds or 0.0
                busy += served
                clock += served
            last = max(last, clock)
    return busy / (last - first) if last > first else 0.0


def all_steps(task: TaskSpec) -> list[Step]:
    return [step for b in range(task.chain_count) for step in task.steps(b)]


def account(name: str, **limits: int) -> AccountSpec:
    """Accounts whose own limits never bind: the general set measures how L4 shares the pool."""
    settings = {
        "budget_units": 1_000_000_000,
        "max_inflight": 64,
        "requests_per_window": 1_000_000,
        "tokens_per_window": 1_000_000_000,
    }
    return AccountSpec(name, **{**settings, **limits})


def finish_task(task: TaskSpec, give_up: float = 10.0) -> TaskSpec:
    """Set max_tokens, a give-up deadline, and a root budget covering worst cases and charges.

    The deadline is when a client abandons the task, not its service objective: SLOs are
    scored from slowdown, so the deadline only bounds how long hopeless work keeps queueing.
    """
    task = replace(task, max_tokens=tokens(MAX_TOKENS))
    ideal = ideal_seconds(task)
    # Charges if every cached prompt missed, the most a root can be billed.
    charges = sum(
        (step.cost_units or 0) + math.ceil(step.cached_tokens * step.cache_discount)
        for step in all_steps(task)
    )
    # The gateway admits a root on worst cases (nothing cached, max_tokens produced); the
    # budget covers them so it never binds before the charges do. +1 covers the mock
    # tokenizer's rounding.
    rates = GATEWAY_PRICES
    worst = sum(
        math.ceil(
            (step.input_tokens + 1) * max(1.0, rates.cache_write) + task.max_tokens * rates.output
        )
        for step in all_steps(task)
    )
    budget = max(worst, charges) * 11 // 10
    return replace(
        task,
        deadline_seconds=round(max(give_up * ideal, ideal + 900), 1),
        budget_units=min(10_000_000, budget),
    )


# ---------------------------------------------------------------- synthetic


def _draw(rng: random.Random, shape: tuple[float, float], cap: float) -> float:
    return min(cap, rng.lognormvariate(*shape))


def sample_step(
    rng: random.Random,
    think: bool = True,
    output: int | None = None,
    prompt: int | None = None,
) -> Step:
    prompt = prompt or round(_draw(rng, INPUT, 900_000))
    output = output or round(_draw(rng, OUTPUT, MAX_TOKENS))
    pause = _draw(rng, THINK, SPLIT_GAP) if think else 0.0
    return make_step(prompt, output, round(prompt * FACTS["cached_fraction"]), think=pause)


def sample_rounds(rng: random.Random, cap: int = 120) -> int:
    return max(1, min(cap, round(rng.lognormvariate(*ROUNDS))))


def sample_task(
    rng: random.Random,
    account_id: str,
    root: str,
    start: float,
    rounds: int | None = None,
    chains: int = 1,
    kind: str = "agent",
    **step_options: int,
) -> TaskSpec:
    count = rounds or sample_rounds(rng)
    built = tuple(
        Chain(0.0, tuple(sample_step(rng, think=r > 0, **step_options) for r in range(count)))
        for _ in range(chains)
    )
    return finish_task(
        TaskSpec(account_id, root, start_at=round(start, 3), kind=kind, chains=built)
    )


def poisson_tasks(
    rng: random.Random,
    shares: dict[str, float],
    target_slots: float,
    span: float = SPAN,
    max_requests: int = MAX_REQUESTS,
    **task_options: int,
) -> tuple[list[TaskSpec], float]:
    """Poisson task arrivals, accounts drawn by share, offering `target_slots` busy slots.

    The gateway admits about 1000 records per run, so when the target would need more
    requests than `max_requests` the arrival window shortens instead of the rate dropping;
    the returned window is the one to divide offered work by.
    """
    probe = [sample_task(rng, "p", "p", 0, **task_options) for _ in range(400)]
    per_task = offered_slots(probe, 1.0) / len(probe)
    steps_per_task = sum(len(all_steps(t)) for t in probe) / len(probe)
    rate = target_slots / per_task
    window = min(span, 0.85 * max_requests / (rate * steps_per_task))
    names, weights = list(shares), list(shares.values())
    tasks: list[TaskSpec] = []
    clock, requests = 0.0, 0
    while True:
        clock += rng.expovariate(rate)
        if clock >= window:
            return tasks, window
        name = rng.choices(names, weights)[0]
        task = sample_task(rng, name, f"{name}-t{len(tasks):04d}", clock, **task_options)
        requests += len(all_steps(task))
        if requests > max_requests:
            return tasks, window
        tasks.append(task)


def skewed_shares(users: int) -> dict[str, float]:
    """Aether's per-user request shares, truncated to `users` and renormalized."""
    shares = USER_SHARES[:users] + (USER_SHARES[-1],) * max(0, users - len(USER_SHARES))
    total = sum(shares)
    return {f"user{i:02d}": share / total for i, share in enumerate(shares)}


# ---------------------------------------------------------------- traces


@dataclass(frozen=True)
class Segment:
    user: str
    rows: tuple[TraceRequest, ...]

    @property
    def start(self) -> float:
        return self.rows[0].start


def usable(rows: list[TraceRequest]) -> list[TraceRequest]:
    return [r for r in rows if r.status == "completed" and r.service > 0 and r.input_tokens > 0]


def _split(user: str, rows: list[TraceRequest]) -> list[Segment]:
    segments, current = [], [rows[0]]
    for row in rows[1:]:
        if row.start - current[-1].end > SPLIT_GAP:
            segments.append(Segment(user, tuple(current)))
            current = []
        current.append(row)
    segments.append(Segment(user, tuple(current)))
    return segments


def lanes(rows: list[TraceRequest]) -> list[Segment]:
    """Sequential chains recovered from per-key request streams.

    A key's overlapping requests come from parallel sessions or sub-agents; each request joins
    the lane that went idle most recently before it started, then lanes split at long gaps.
    """
    by_key: dict[str, list[TraceRequest]] = defaultdict(list)
    for row in rows:
        by_key[row.key].append(row)
    segments: list[Segment] = []
    for key_rows in by_key.values():
        open_lanes: list[list[TraceRequest]] = []
        for row in sorted(key_rows, key=lambda r: r.start):
            idle = [lane for lane in open_lanes if lane[-1].end <= row.start]
            if idle:
                max(idle, key=lambda lane: lane[-1].end).append(row)
            else:
                open_lanes.append([row])
        for lane in open_lanes:
            segments.extend(_split(lane[0].user, lane))
    return segments


def sessions(rows: list[TraceRequest]) -> list[Segment]:
    """abyss chains: one per session and sub-agent, split at long gaps."""
    by_chain: dict[tuple[str, str], list[TraceRequest]] = defaultdict(list)
    for row in rows:
        by_chain[(row.session, row.chain)].append(row)
    segments: list[Segment] = []
    for chain_rows in by_chain.values():
        segments.extend(_split(chain_rows[0].user, sorted(chain_rows, key=lambda r: r.start)))
    return segments


def segment_task(segment: Segment, account_id: str, root: str, start_at: float) -> TaskSpec:
    steps, previous_end = [], None
    for row in segment.rows:
        think = 0.0 if previous_end is None else max(0.0, row.start - previous_end)
        steps.append(
            make_step(row.input_tokens, row.output_tokens, row.cached_tokens,
                      row.cache_write_tokens, row.service, think, row.model)
        )  # fmt: skip
        previous_end = row.end
    return finish_task(
        TaskSpec(account_id, root, start_at=round(start_at, 3), kind="trace",
                 chains=(Chain(0.0, tuple(steps)),))
    )  # fmt: skip


@dataclass(frozen=True)
class Hour:
    start: float
    rows: tuple[TraceRequest, ...]

    @property
    def users(self) -> int:
        return len({r.user for r in self.rows})

    @property
    def offered(self) -> float:
        return sum(r.service for r in self.rows) / SPAN


def eligible_hours(
    rows: list[TraceRequest], min_users: int = 3, min_offered: float = 2.0
) -> list[Hour]:
    """Clock hours with real multi-user contention."""
    by_hour: dict[int, list[TraceRequest]] = defaultdict(list)
    for row in usable(rows):
        by_hour[int(row.start // SPAN)].append(row)
    hours = [Hour(h * SPAN, tuple(v)) for h, v in sorted(by_hour.items())]
    return [h for h in hours if h.users >= min_users and h.offered >= min_offered]


def hours_workload(hours: list[Hour], label: str, max_requests: int = MAX_REQUESTS) -> Workload:
    """Overlay real hours on one platform; each hour's users stay distinct accounts."""
    tasks: list[TaskSpec] = []
    for index, hour in enumerate(hours):
        segments = sorted(lanes(list(hour.rows)), key=lambda s: s.start)
        for number, segment in enumerate(segments):
            name = f"h{index}u{segment.user[:5]}"
            tasks.append(
                segment_task(segment, name, f"{name}-l{number:03d}", segment.start - hour.start)
            )
    tasks.sort(key=lambda t: t.start_at)
    kept, total = [], 0
    for task in tasks:
        size = len(all_steps(task))
        if total + size <= max_requests:
            kept.append(task)
            total += size
    names = sorted({t.account_id for t in kept})
    return Workload(
        tuple(account(n) for n in names),
        tuple(kept),
        {"source": label, "hours": [h.start for h in hours], "requests": total, "span": SPAN},
    )


def abyss_workload(
    rows: list[TraceRequest], seed: int, users: int, max_requests: int = MAX_REQUESTS
) -> Workload:
    """Single-user sessions dealt to `users` accounts by Aether's skew, starting at Poisson
    times; each segment keeps its real internal timing and is cut to half the span."""
    rng = random.Random(seed)
    segments = sessions(usable(rows))
    rng.shuffle(segments)
    shares = skewed_shares(users)
    names, weights = list(shares), list(shares.values())
    tasks: list[TaskSpec] = []
    total = 0
    for index, segment in enumerate(segments):
        kept = tuple(r for r in segment.rows if r.end - segment.start <= SPAN / 2)
        if not kept or total + len(kept) > max_requests:
            continue
        name = rng.choices(names, weights)[0]
        tasks.append(
            segment_task(Segment(segment.user, kept), name, f"{name}-s{index:04d}",
                         rng.uniform(0, SPAN * 0.8))
        )  # fmt: skip
        total += len(kept)
        if total >= max_requests * 0.95:
            break
    names_used = sorted({t.account_id for t in tasks})
    return Workload(
        tuple(account(n) for n in names_used),
        tuple(sorted(tasks, key=lambda t: t.start_at)),
        {"source": "abyss", "seed": seed, "users": users, "requests": total, "span": SPAN},
    )


def capacity_for(offered: float, load: float) -> int:
    # Two slots at least, so there is a split to be fair or unfair about.
    return max(2, min(64, round(offered / load)))
