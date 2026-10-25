"""The two test sets: general samples of realistic load, and adversarial probes.

General samples are real or calibrated mixes run at two utilizations; they measure how well
L4 serves ordinary traffic. Probes each exaggerate one feature observed in the traces (fan-out,
decode-heavy output, bursts, outages, ...) and judge one claim with one number.
"""

import asyncio
import random
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from l4kit import Behavior, Chain, Result, ScriptedPool, TaskSpec, Workload, history
from l4kit.pool import Fault, Placement, PoolRejected

from taskloom.contracts.models import (
    GatewayRecord,
    ModelReply,
    ModelRequest,
    PlatformConfig,
    PoolAccount,
)
from taskloom.policies.scheduling import PolicyName

from . import calibrate as cal
from . import traces
from .facts import FACTS, USER_SHARES, cache_retention

# Nominal offered utilization. Closed-loop chains slow down while they queue, so the
# realized load is lower; 1.0 is near saturation, 2.0 is sustained overload.
GENERAL_LOADS = (1.0, 2.0)
FEEDBACK_LOAD = 2.0
SYNTHETIC_SLOTS = 16.0
# The platform pool is several upstream accounts, each with its own slots and prompt cache
# (Aether: 71 keys of 8 providers), so where a follow-up lands decides what it pays.
SLOTS_PER_ACCOUNT = 2
# Who picks the upstream account. "gateway": the PlatformConfig lists the pool's accounts and
# the gateway places every call (its cache-affinity default). "rotate" / "sticky": the config
# lists none, as before WP5, so the gateway names no account and the pool's stand-in places.
# bench.py sets these per run in each worker, so configs and pools read them when built.
PoolPlacement = Literal["gateway", "rotate", "sticky"]
PLACEMENT: PoolPlacement = "gateway"
POLICY: PolicyName = "hybrid"
# PlatformConfig fields every case sets (bench.py run --set), for development sweeps.
OVERRIDES: dict[str, Any] = {}


@dataclass
class Case:
    workload: Workload
    config: PlatformConfig
    pool: ScriptedPool
    coordinated: bool = True
    horizon: float = 4 * cal.SPAN
    duplicate_rate: float = 0.0
    seed: int = 0
    info: dict[str, Any] = field(default_factory=dict)


def platform(capacity: int, **overrides: Any) -> PlatformConfig:
    """Generous queues, windows and budgets, so the pool is the contended resource."""
    settings: dict[str, Any] = {
        "pool_capacity": capacity,
        "scheduling_policy": POLICY,
        "max_queue": 1024,
        "max_tenant_queue": 256,
        "max_queue_bytes": 64 * 2**20,
        "max_history_bytes": 256 * 2**20,
        "requests_per_window": 1_000_000,
        "tokens_per_window": 1_000_000_000,
        "total_budget_units": 1_000_000_000_000,
        "output_price": cal.GATEWAY_PRICES.output,
        "cache_read_price": cal.GATEWAY_PRICES.cache_read,
        "cache_write_price": cal.GATEWAY_PRICES.cache_write,
    }
    if PLACEMENT == "gateway":
        # Same ids and slots as pool(capacity).account_ids.
        settings["pool_accounts"] = [
            PoolAccount(account_id=f"a{index}", capacity=slots)
            for index, slots in enumerate(split(capacity))
        ]
    return PlatformConfig(**{**settings, **OVERRIDES, **overrides})


def split(capacity: int) -> tuple[int, ...]:
    """Slots per upstream account: at least two accounts of about SLOTS_PER_ACCOUNT, even."""
    count = min(capacity, max(2, -(-capacity // SLOTS_PER_ACCOUNT)))
    base, extra = divmod(capacity, count)
    return (base + 1,) * extra + (base,) * (count - extra)


def pool(capacity: int, cls: type[ScriptedPool] = ScriptedPool, **options: Any) -> ScriptedPool:
    placement: Placement = "rotate" if PLACEMENT == "gateway" else PLACEMENT
    return cls(accounts=split(capacity), placement=placement, retention=cache_retention, **options)


def workload(tasks: list[TaskSpec], **meta: Any) -> Workload:
    names = sorted({t.account_id for t in tasks})
    return Workload(
        tuple(cal.account(n) for n in names), tuple(sorted(tasks, key=lambda t: t.start_at)), meta
    )


def at_load(source: Workload, load: float, coordinated: bool, seed: int = 0) -> Case:
    offered = cal.ideal_concurrency(source.tasks)
    capacity = cal.capacity_for(offered, load)
    return Case(
        source, platform(capacity), pool(capacity), coordinated, seed=seed,
        info={"offered": offered, "load": offered / capacity},
    )  # fmt: skip


# ---------------------------------------------------------------- general set


@dataclass(frozen=True)
class Sample:
    family: str  # aether | abyss | synthetic
    index: int

    @property
    def name(self) -> str:
        return f"{self.family}-{self.index:02d}"


def _hour_pairs(count: int) -> list[tuple[int, int]]:
    hours = cal.eligible_hours(traces.load(traces.TRACE_DIR / "aether.csv"))
    rng = random.Random(0)
    pairs: list[tuple[int, int]] = []
    while len(pairs) < count:
        a, b = sorted(rng.sample(range(len(hours)), 2))
        if (a, b) not in pairs and len(hours[a].rows) + len(hours[b].rows) <= 900:
            pairs.append((a, b))
    return pairs


def general_workload(sample: Sample) -> Workload:
    if sample.family == "aether":
        hours = cal.eligible_hours(traces.load(traces.TRACE_DIR / "aether.csv"))
        a, b = _hour_pairs(sample.index + 1)[sample.index]
        return cal.hours_workload([hours[a], hours[b]], sample.name)
    if sample.family == "synthetic":
        # Same users and skew as the abyss deals; long chains keep ideal concurrency near 4.
        rng = random.Random(sample.index)
        tasks, window = cal.poisson_tasks(rng, cal.skewed_shares(8), SYNTHETIC_SLOTS)
        return workload(tasks, source="synthetic", seed=sample.index, span=window)
    if sample.family == "abyss":
        users = (4, 8, 16)[sample.index % 3]
        return cal.abyss_workload(
            traces.load(traces.TRACE_DIR / "abyss.csv"), seed=sample.index, users=users
        )
    raise ValueError(sample.family)


def general_case(sample: Sample, load: float, coordinated: bool) -> Case:
    return at_load(general_workload(sample), load, coordinated, sample.index)


GENERAL: dict[str, tuple[str, ...]] = {
    "aether": tuple(Sample("aether", i).name for i in range(16)),
    "abyss": tuple(Sample("abyss", i).name for i in range(8)),
    "synthetic": tuple(Sample("synthetic", i).name for i in range(8)),
}


def sample_named(name: str) -> Sample:
    family, index = name.rsplit("-", 1)
    return Sample(family, int(index))


# ---------------------------------------------------------------- pools with faults


@dataclass
class FaultyPool(ScriptedPool):
    """Faults at fixed rates (refusals per dispatch, lost replies per attempt), plus outages."""

    rates: dict[Fault, float] = field(default_factory=dict)
    outages: tuple[tuple[float, float], ...] = ()  # loop-time [start, end)
    seed: int = 0

    async def generate(self, request: ModelRequest, account: str | None = None) -> ModelReply:
        now = asyncio.get_running_loop().time()
        behavior = self.behaviors.get(request.attempt_id, Behavior())
        # A refusal reflects the pool's state when the request arrives, not a trait of the
        # request: outages refuse whatever comes while they last, and each later dispatch of a
        # refused request draws again. Lost replies stay tied to their single execution.
        refused = self.refusals[request.request_id]
        if any(start <= now < end for start, end in self.outages):
            self.refusals[request.request_id] += 1
            raise PoolRejected("pool outage")
        if behavior.fault == "none":
            fault: Fault = "none"
            tag = f"{self.seed}:{request.attempt_id}" + (f":{refused}" if refused else "")
            draw = random.Random(tag).random()
            for name, rate in self.rates.items():
                if draw < rate:
                    fault = name
                    break
                draw -= rate
            if fault == "reject":
                self.refusals[request.request_id] += 1
                raise PoolRejected("pool busy")
            if fault != "none":
                self.behaviors[request.attempt_id] = replace(behavior, fault=fault)
        return await super().generate(request, account)


# ---------------------------------------------------------------- adversarial probes


@dataclass(frozen=True)
class Probe:
    name: str
    fact: str  # the observed feature the probe exaggerates
    claim: str  # what a correct L4 guarantees under it
    build: Callable[[int, str], Case]  # (seed, variant) -> case
    judge: Callable[[dict[str, Any], dict[str, Any] | None], float]  # (attack, reference) -> 0..1
    extract: Callable[[Result, Case], dict[str, Any]] | None = None
    paired: bool = False  # also runs a "reference" variant without the adversary


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _linear(value: float, good: float, bad: float) -> float:
    """1 at `good`, 0 at `bad`, linear between."""
    return _clamp((bad - value) / (bad - good))


def _occupancy(result: Result) -> dict[str, float]:
    owner = {t.spec.root_task_id: t.spec.account_id for t in result.tasks}
    held: dict[str, float] = {}
    for call in result.pool.log:
        name = owner.get(call.root_task_id, "?")
        held[name] = held.get(name, 0.0) + call.finished - call.started
    return held


def _fairness_judge(attack: dict[str, Any], reference: dict[str, Any] | None) -> float:
    # Half the busy slots misallocated is the worst case scored as zero.
    return _linear(attack["account_unfairness"], 0.05, 0.5)


def _attacker_share(result: Result, case: Case) -> dict[str, Any]:
    held = _occupancy(result)
    total = sum(held.values()) or 1.0
    return {"attacker_share": held.get("attacker", 0.0) / total, "accounts": len(held)}


def _contenders(
    rng: random.Random, chains: int, rounds: int, prefix: str = "honest"
) -> list[TaskSpec]:
    return [
        cal.sample_task(rng, f"{prefix}{i}", f"{prefix}{i}-root", 0, rounds, chains)
        for i in range(3)
    ]


def fanout_agents(seed: int, variant: str) -> Case:
    rng = random.Random(seed)
    peak = int(FACTS["user_peak_concurrency"])
    tasks = [
        *_contenders(rng, 2, 20),
        cal.sample_task(rng, "attacker", "attacker-root", 0, 20, chains=peak),
    ]
    return Case(workload(tasks), platform(8), pool(8), seed=seed)


def fanout_roots(seed: int, variant: str) -> Case:
    rng = random.Random(seed)
    peak = int(FACTS["user_peak_concurrency"])
    tasks = [
        *_contenders(rng, 2, 20),
        *(cal.sample_task(rng, "attacker", f"attacker-r{i:02d}", 0, 20) for i in range(peak)),
    ]
    return Case(workload(tasks), platform(8), pool(8), seed=seed)


def decode_heavy(seed: int, variant: str) -> Case:
    rng = random.Random(seed)
    heavy = int(FACTS["output_p99"])
    tasks = [
        *_contenders(rng, 4, 15),
        cal.sample_task(rng, "attacker", "attacker-root", 0, 15, chains=4, output=heavy),
    ]
    return Case(workload(tasks), platform(8), pool(8), seed=seed)


def _victims(rng: random.Random, slots: float) -> list[TaskSpec]:
    shares = {f"victim{i}": 1.0 for i in range(5)}
    return cal.poisson_tasks(rng, shares, slots, span=1800, max_requests=500)[0]


def burst(seed: int, variant: str) -> Case:
    rng = random.Random(seed)
    tasks = _victims(rng, 0.6 * 8)
    if variant == "attack":
        per_minute = int(FACTS["user_burst_per_minute"])
        tasks += [
            cal.sample_task(rng, "attacker", f"attacker-t{i:03d}", 300 + 60 * i / per_minute, 2)
            for i in range(per_minute)
        ]
    return Case(workload(tasks), platform(8), pool(8), seed=seed)


def _victim_slo(result: Result, case: Case) -> dict[str, Any]:
    from .metrics import slo_credit

    credits = []
    for task in result.tasks:
        if task.spec.account_id.startswith("victim"):
            value = (
                (task.finished - task.started) / cal.ideal_seconds(task.spec)
                if task.ok and task.finished is not None
                else None
            )
            credits.append(slo_credit(value))
    return {"victim_slo": sum(credits) / max(1, len(credits))}


def _burst_judge(attack: dict[str, Any], reference: dict[str, Any] | None) -> float:
    assert reference is not None
    ratio = attack["victim_slo"] / max(reference["victim_slo"], 1e-9)
    return _linear(ratio, 0.95, 0.5)


def skew(seed: int, variant: str) -> Case:
    rng = random.Random(seed)
    shares = {f"user{i:02d}": s for i, s in enumerate(USER_SHARES)}
    tasks, _ = cal.poisson_tasks(rng, shares, 1.2 * 8, span=1800)
    return Case(workload(tasks), platform(8), pool(8), seed=seed)


def _light_slo(result: Result, case: Case) -> dict[str, Any]:
    from .metrics import measure

    by_account = measure(result)["slo_by_account"]
    light = [f"user{i:02d}" for i in range(len(USER_SHARES) // 2, len(USER_SHARES))]
    values = [by_account[name] for name in light if name in by_account]
    return {"light_slo": sum(values) / max(1, len(values))}


# Outage episodes: 476 of 694 lasted one minute and 106 two; two-minute outages a quarter
# and half way in are mildly adversarial without making a fail-fast platform look broken.
OUTAGES = tuple((start, start + 120.0) for start in (450.0, 900.0))
AFTER_OUTAGES = max(end for _, end in OUTAGES)


def outage(seed: int, variant: str) -> Case:
    rng = random.Random(seed)
    tasks, _ = cal.poisson_tasks(rng, {f"user{i}": 1.0 for i in range(6)}, 0.7 * 8, span=1800)
    if variant == "reference":
        return Case(workload(tasks), platform(8), pool(8), seed=seed)
    background = FACTS["fault_reject"] * (1 - FACTS["outage_failure_share"])
    faulty = pool(
        8, FaultyPool,
        rates={"reject": background, "lost": FACTS["fault_lost"],
               "lost_forever": FACTS["fault_lost_forever"]},
        outages=OUTAGES,
        seed=seed,
    )  # fmt: skip
    return Case(workload(tasks), platform(8), faulty, seed=seed, horizon=3 * 1800)


def _stuck_unknown(result: Result) -> list[GatewayRecord]:
    """Requests unknown when the workload ended (not when shutdown cancelled the rest)
    whose outcome the pool could still report."""
    service = result.service
    return [
        r for r in history(service)
        if result.final_states.get(service.key(r.tenant_id, r.request.request_id)) == "UNKNOWN"
        and result.pool.behaviors.get(r.request.attempt_id, Behavior()).fault != "lost_forever"
    ]  # fmt: skip


def _outage_effects(result: Result, case: Case) -> dict[str, Any]:
    # A slot is leaked when its request ended unknown although the pool can answer for it;
    # slots held for lost_forever replies are kept on purpose until reconciliation.
    leaked = _stuck_unknown(result)
    later = [t for t in result.tasks if t.spec.start_at >= AFTER_OUTAGES]
    return {
        "leaked_slots": len(leaked),
        "post_outage_tasks": len(later),
        "post_outage_completed": sum(t.ok for t in later) / max(1, len(later)),
        "capacity": case.config.pool_capacity,
    }


def _outage_judge(attack: dict[str, Any], reference: dict[str, Any] | None) -> float:
    assert reference is not None
    kept = 1 - attack["leaked_slots"] / attack["capacity"]
    recovered = attack["post_outage_completed"] / max(reference["post_outage_completed"], 1e-9)
    return _clamp(kept) * _linear(recovered, 0.95, 0.5)


def retransmit(seed: int, variant: str) -> Case:
    rng = random.Random(seed)
    tasks, _ = cal.poisson_tasks(rng, {f"user{i}": 1.0 for i in range(6)}, 0.6 * 8, span=1800)
    tasks = [
        replace(t, cancel_after=rng.uniform(5, 120)) if rng.random() < 0.1 else t for t in tasks
    ]
    faulty = pool(8, FaultyPool, rates={"lost": 0.05, "lost_forever": 0.01}, seed=seed)
    return Case(
        workload(tasks), platform(8), faulty, duplicate_rate=0.1, seed=seed, horizon=3 * 1800
    )


def _unresolved(result: Result, case: Case) -> dict[str, Any]:
    return {"unresolved": len(_stuck_unknown(result))}


def _retransmit_judge(attack: dict[str, Any], reference: dict[str, Any] | None) -> float:
    # Exactly-once billing is checked by the consistency gate; here every reconcilable
    # request must also have been reconciled.
    return 1.0 if attack["unresolved"] == 0 else 0.0


ENDURANCE_TASKS = 1400
ENDURANCE_TAIL = 200


def endurance(seed: int, variant: str) -> Case:
    rng = random.Random(seed)
    tasks = [
        replace(
            cal.sample_task(rng, f"user{i % 4}", f"user{i % 4}-t{i:04d}", i * 3.6, 1,
                            prompt=2_000, output=40),
            deadline_seconds=60,  # a short give-up keeps rejected clients from retrying long
        )
        for i in range(ENDURANCE_TASKS)
    ]  # fmt: skip
    return Case(workload(tasks), platform(4), pool(4), seed=seed, horizon=7200)


def _tail_done(result: Result, case: Case) -> dict[str, Any]:
    ordered = sorted(result.tasks, key=lambda t: t.spec.start_at)[-ENDURANCE_TAIL:]
    return {"tail_completed": sum(t.ok for t in ordered) / len(ordered)}


def budget_tight(seed: int, variant: str) -> Case:
    rng = random.Random(seed)
    tasks = []
    for i in range(4):
        for t in range(6):
            steps = tuple(
                cal.make_step(prompt, round(cal._draw(rng, cal.OUTPUT, cal.MAX_TOKENS)) * 4,
                              round(prompt * FACTS["cached_fraction"]), model="gpt-6", think=1.0)
                for prompt in (round(cal._draw(rng, cal.INPUT, 900_000)) for _ in range(8))
            )  # fmt: skip
            tasks.append(cal.finish_task(
                TaskSpec(f"user{i}", f"user{i}-t{t}", start_at=t * 60.0, chains=(Chain(0, steps),))
            ))  # fmt: skip
    # Budgets are half the planned charges. Cache misses on a moved chain raise what the pool
    # truly bills, which only makes the budget bind sooner: those are real charges, and the
    # claim is that whatever is billed stays within the budget.
    accounts = []
    for name in sorted({t.account_id for t in tasks}):
        charge = sum(
            s.cost_units or 0 for t in tasks if t.account_id == name for s in cal.all_steps(t)
        )
        accounts.append(cal.account(name, budget_units=charge // 2))
    source = Workload(tuple(accounts), tuple(sorted(tasks, key=lambda t: t.start_at)), {})
    # Every request is a GPT model, which bills a missed prompt as fresh input.
    return Case(source, platform(8, cache_write_price=1.0), pool(8), seed=seed)


def _budget_use(result: Result, case: Case) -> dict[str, Any]:
    spent: dict[str, int] = {}
    for record in history(result.service):
        spent[record.tenant_id] = spent.get(record.tenant_id, 0) + record.consumed_units
    budgets = {a.account_id: a.budget_units for a in case.workload.accounts}
    used = [min(1.0, spent.get(n, 0) / b) for n, b in budgets.items()]
    over = [max(0, spent.get(n, 0) - b) / b for n, b in budgets.items()]
    return {"budget_used": sum(used) / len(used), "budget_over": max(over)}


def _budget_judge(attack: dict[str, Any], reference: dict[str, Any] | None) -> float:
    # Spend what the budget allows without overrunning it: 90% use is full marks.
    return _linear(attack["budget_used"], 0.9, 0.3) * _linear(attack["budget_over"], 0.0, 0.1)


WARM = "warm"
# Ten 10-round chains of six users, so the first moves of a chain onto accounts it has never
# used weigh in (after visiting all four, even round-robin finds warm caches). Warm and bulk
# traffic together offer half the pool; realized load in the warm window is 0.5-0.7, which
# fills a chain's two-slot account for 10-25% of its follow-ups. Placement in front of the
# pool cannot hold a request for its account, so near saturation it moves most chains
# whatever it prefers, and the probe would score queueing rather than affinity.
AFFINITY_CHAINS = 10
AFFINITY_ROUNDS = 10
AFFINITY_LOAD = 0.5


def cache_affinity(seed: int, variant: str) -> Case:
    """Long chains with large cached contexts amid bulk traffic.

    A follow-up's warm account is sometimes full, so a placement either waits for it or moves
    the chain to an account that holds little or none of its cache.
    """
    rng = random.Random(seed)
    # Warm chains start once bulk tasks, minutes long each, have built up their load.
    tasks = [
        cal.sample_task(rng, f"{WARM}{i % 6}", f"{WARM}{i % 6}-t{i}", rng.uniform(600, 900),
                        AFFINITY_ROUNDS, prompt=rng.randint(150_000, 250_000))
        for i in range(AFFINITY_CHAINS)
    ]  # fmt: skip
    span = max(cal.ideal_seconds(t) + t.start_at for t in tasks)
    warm_slots = cal.offered_slots(tasks, span)
    tasks += cal.poisson_tasks(
        rng, {f"bulk{i}": 1.0 for i in range(4)}, max(1.0, AFFINITY_LOAD * 8 - warm_slots),
        span=span, max_requests=cal.MAX_REQUESTS - AFFINITY_ROUNDS * len(tasks),
    )[0]  # fmt: skip
    return Case(workload(tasks), platform(8), pool(8), seed=seed)


def _warm_retention(result: Result, case: Case) -> dict[str, Any]:
    """Cache kept by the warm chains' follow-ups, which bulk traffic would dilute."""
    follow = [
        call for call in result.pool.log
        if call.root_task_id.startswith(WARM) and not call.first and call.cached_tokens > 0
    ]  # fmt: skip
    cached = sum(call.cached_tokens for call in follow)
    kept = sum(call.cached_tokens * call.retained for call in follow)
    return {
        "warm_retention": kept / cached if cached else 1.0,
        "warm_moves": sum(call.moved for call in follow) / max(1, len(follow)),
        "warm_followers": len(follow),
    }


def _affinity_judge(attack: dict[str, Any], reference: dict[str, Any] | None) -> float:
    return _linear(attack["warm_retention"], 0.9, 0.5)


ELEPHANT = "elephant-root"
WINDOW = 60.0


def elephant(seed: int, variant: str) -> Case:
    rng = random.Random(seed)
    small = [
        cal.finish_task(TaskSpec(
            f"small{i}", f"small{i}-root",
            chains=(Chain(rng.uniform(0, 5), tuple(
                cal.make_step(100_000, 100, 92_000) for _ in range(40))),),
        ), give_up=30)
        for i in range(6)
    ]  # fmt: skip
    big = cal.finish_task(
        TaskSpec("elephant", ELEPHANT, start_at=60,
                 chains=(Chain(0.0, (cal.make_step(892_000, 400, 0),)),)),
        give_up=200,
    )  # fmt: skip
    small_reserve = small[0].chains[0].steps[0].input_tokens + small[0].max_tokens
    # Eight small reservations fill a window; the elephant fits only behind at most one.
    config = platform(4, tokens_per_window=8 * small_reserve, window_seconds=WINDOW)
    return Case(workload([*small, big]), config, pool(4), seed=seed)


def _elephant_wait(result: Result, case: Case) -> dict[str, Any]:
    from .timeline import lives

    # Rejected or abandoned counts as never served, not as a wait of zero.
    served = any(t.ok for t in result.tasks if t.spec.root_task_id == ELEPHANT)
    waits = [life.wait for life in lives(result) if life.root == ELEPHANT]
    return {"elephant_windows": waits[0] / WINDOW if served and waits else float("inf")}


def _elephant_judge(attack: dict[str, Any], reference: dict[str, Any] | None) -> float:
    return _linear(attack["elephant_windows"], 2.0, 10.0)


def namespace(seed: int, variant: str) -> Case:
    rng = random.Random(seed)
    victims = [cal.sample_task(rng, f"victim{i}", f"shared-{i}", 0, 10) for i in range(3)]
    # Same root names under another account, with budgets too small for a single request.
    forged = [
        replace(cal.sample_task(rng, "attacker", f"shared-{i}", 0, 10), budget_units=1)
        for i in range(3)
    ]
    return Case(workload([*victims, *forged]), platform(4), pool(4), seed=seed)


def _victims_done(result: Result, case: Case) -> dict[str, Any]:
    victims = [t for t in result.tasks if t.spec.account_id.startswith("victim")]
    return {"victims_done": sum(t.ok for t in victims) / max(1, len(victims))}


def _namespace_judge(attack: dict[str, Any], reference: dict[str, Any] | None) -> float:
    return 1.0 if attack["victims_done"] == 1.0 else 0.0


PROBES: dict[str, Probe] = {
    p.name: p
    for p in (
        Probe("fanout-agents", "one user peaked at 16 parallel requests",
              "16 sub-agents in one root earn no more than a max-min share",
              fanout_agents, _fairness_judge, _attacker_share),
        Probe("fanout-roots", "one user peaked at 16 parallel requests",
              "16 roots earn no more than a max-min share", fanout_roots, _fairness_judge,
              _attacker_share),
        Probe("decode-heavy", "P99 output is 5.3k tokens, ~140 s of a slot",
              "long decodes do not crowd out others' slot time", decode_heavy, _fairness_judge,
              _attacker_share),
        Probe("burst", "one user started 81 requests in one minute",
              "a burst does not degrade other users' SLO", burst, _burst_judge, _victim_slo,
              paired=True),
        Probe("skew", "the top user sends 36% of requests",
              "light users keep their SLO under a heavy one", skew,
              lambda a, r: a["light_slo"], _light_slo),
        Probe("outage", "70% of failures fall in outage minutes, mostly 1-2 min",
              "capacity and progress recover after an outage", outage, _outage_judge,
              _outage_effects, paired=True),
        Probe("retransmit", "lost replies and duplicate transmissions",
              "every reconcilable request settles exactly once", retransmit, _retransmit_judge,
              _unresolved),
        Probe("endurance", "one user sent 93k requests over 3.5 months",
              "admission keeps working past any fixed count", endurance,
              lambda a, r: a["tail_completed"], _tail_done),
        Probe("budget-tight", "output is priced 5x input, cache reads 0.1x",
              "a budget is spent fully but never overrun", budget_tight, _budget_judge,
              _budget_use),
        Probe("cache-affinity",
              "a chain moved to an upstream account it has not used keeps ~12% of its cache",
              "chains stay on the account holding their cache without starving",
              cache_affinity, _affinity_judge, _warm_retention),
        Probe("elephant", "prompts reach 900k tokens against shared token windows",
              "a large reservation is not starved by small ones", elephant, _elephant_judge,
              _elephant_wait),
        Probe("namespace", "root ids are chosen by clients",
              "another account's identical root ids cannot touch yours", namespace,
              _namespace_judge, _victims_done),
    )
}  # fmt: skip
