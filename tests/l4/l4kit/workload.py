"""Workload description: who submits which root tasks, and how each Agent chain behaves.

A Workload is plain data so generated scenarios can be saved as JSON traces and replayed.
"""

import json
import math
import random
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AccountSpec:
    account_id: str
    budget_units: int = 250000
    max_inflight: int = 1
    requests_per_window: int = 60
    tokens_per_window: int = 50000


@dataclass(frozen=True)
class Step:
    """One model call of a replayed chain; `think_seconds` is the pause before sending it."""

    input_tokens: int
    output_tokens: int
    cost_units: int | None = None  # charge with the trace's cache hits
    service_seconds: float | None = None
    think_seconds: float = 0.0
    cached_tokens: int = 0  # prompt tokens the trace read from cache
    cache_discount: float = 0.0  # extra cost units per cached token that misses
    cold_seconds: float = 0.0  # extra prefill time if the whole cached prompt misses


@dataclass(frozen=True)
class Chain:
    """An Agent chain that starts `offset` seconds after its root task."""

    offset: float
    steps: tuple[Step, ...]


@dataclass(frozen=True)
class TaskSpec:
    """One root task: `branches` parallel Agent chains of `rounds` model calls each.

    Context grows by `input_growth` tokens per round; `think_seconds` stands in for
    the tool and L2 work between two model calls of a chain. Explicit `chains`, as
    built from traces, replace that parametric shape.
    """

    account_id: str
    root_task_id: str
    start_at: float = 0
    kind: str = "interactive"
    branches: int = 1
    rounds: int = 2
    input_tokens: int = 200
    input_growth: int = 100
    max_tokens: int = 256
    output_tokens: int = 64
    think_seconds: float = 0.5
    deadline_seconds: float = 120
    budget_units: int = 100000
    cancel_after: float | None = None
    chains: tuple[Chain, ...] = ()

    @property
    def chain_count(self) -> int:
        return len(self.chains) or self.branches

    def steps(self, branch: int) -> tuple[Step, ...]:
        if self.chains:
            return self.chains[branch].steps
        return tuple(
            Step(
                self.request_input_tokens(r),
                self.output_tokens,
                think_seconds=self.think_seconds if r else 0,
            )
            for r in range(self.rounds)
        )

    def request_input_tokens(self, round_index: int) -> int:
        return self.input_tokens + self.input_growth * round_index

    def reserve_upper_bound(self) -> int:
        """Sum of platform reservations if every request of the root is admitted."""
        if self.chains:
            steps = [step for chain in self.chains for step in chain.steps]
            return sum(step.input_tokens + self.max_tokens for step in steps) + len(steps)
        per_branch = sum(self.request_input_tokens(r) + self.max_tokens for r in range(self.rounds))
        return self.branches * per_branch + self.branches * self.rounds


@dataclass(frozen=True)
class Workload:
    accounts: tuple[AccountSpec, ...]
    tasks: tuple[TaskSpec, ...]
    meta: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "Workload":
        data = json.loads(text)
        return cls(
            accounts=tuple(AccountSpec(**a) for a in data["accounts"]),
            tasks=tuple(_task_from_json(t) for t in data["tasks"]),
            meta=data.get("meta", {}),
        )

    def save(self, path: Path) -> None:
        path.write_text(self.to_json())

    @classmethod
    def load(cls, path: Path) -> "Workload":
        return cls.from_json(path.read_text())


def _task_from_json(data: dict[str, Any]) -> TaskSpec:
    chains = tuple(
        Chain(c["offset"], tuple(Step(**step) for step in c["steps"]))
        for c in data.get("chains", ())
    )
    return TaskSpec(**{**data, "chains": chains})


# Task classes for the mixed workload. Token sizes are lognormal (median, sigma) so a few
# code tasks dominate cost, which is where request-count and cost fairness diverge.
CLASSES: dict[str, dict[str, Any]] = {
    "interactive": {
        "weight": 0.6,
        "rounds": (1, 2),
        "branches": (1, 1),
        "input": (300, 0.5),
        "output": (80, 0.5),
        "think": 0.2,
        "deadline": 30,
    },
    "document": {
        "weight": 0.3,
        "rounds": (2, 5),
        "branches": (1, 2),
        "input": (1200, 0.6),
        "output": (200, 0.5),
        "think": 1.0,
        "deadline": 300,
    },
    "code": {
        "weight": 0.1,
        "rounds": (4, 12),
        "branches": (1, 4),
        "input": (3000, 0.8),
        "output": (500, 0.6),
        "think": 3.0,
        "deadline": 900,
    },
}


def _lognormal(rng: random.Random, median: float, sigma: float, cap: int) -> int:
    return max(1, min(cap, round(rng.lognormvariate(math.log(median), sigma))))


def mixed_workload(
    seed: int,
    users: int = 4,
    tasks_per_user: int = 5,
    arrival_seconds: float = 10.0,
    account: AccountSpec | None = None,
) -> Workload:
    """Poisson arrivals per user over the interactive/document/code class mix."""
    rng = random.Random(seed)
    template = account or AccountSpec("template")
    accounts = tuple(
        AccountSpec(
            f"user{u:03d}",
            template.budget_units,
            template.max_inflight,
            template.requests_per_window,
            template.tokens_per_window,
        )
        for u in range(users)
    )
    names = list(CLASSES)
    weights = [CLASSES[name]["weight"] for name in names]
    tasks: list[TaskSpec] = []
    for spec in accounts:
        clock = 0.0
        for index in range(tasks_per_user):
            clock += rng.expovariate(1 / arrival_seconds)
            kind = rng.choices(names, weights)[0]
            shape = CLASSES[kind]
            output = _lognormal(rng, *shape["output"], cap=2048)
            task = TaskSpec(
                account_id=spec.account_id,
                root_task_id=f"{spec.account_id}-t{index:03d}",
                start_at=round(clock, 3),
                kind=kind,
                branches=rng.randint(*shape["branches"]),
                rounds=rng.randint(*shape["rounds"]),
                input_tokens=_lognormal(rng, *shape["input"], cap=8000),
                input_growth=output,
                max_tokens=min(4096, output * 2),
                output_tokens=output,
                think_seconds=shape["think"],
                deadline_seconds=shape["deadline"],
            )
            # Give each root enough budget for its own reservations; budget pressure is
            # a separate scenario, not a side effect of the class mix.
            tasks.append(replace(task, budget_units=min(10_000_000, task.reserve_upper_bound())))
    return Workload(
        accounts,
        tuple(sorted(tasks, key=lambda t: t.start_at)),
        {"generator": "mixed_workload", "seed": seed, "users": users},
    )
