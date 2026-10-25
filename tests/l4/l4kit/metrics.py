"""Per-account and per-root service, waiting and completion derived from platform records."""

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from taskloom.contracts.models import GatewayRecord

from .client import TaskResult


def _ts(value: str | None) -> float | None:
    return datetime.fromisoformat(value).timestamp() if value else None


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1)]


def jain(values: list[float]) -> float:
    """Jain fairness index: 1 is perfectly equal, 1/n is one party taking everything."""
    if not values or not any(values):
        return 1.0
    return sum(values) ** 2 / (len(values) * sum(v * v for v in values))


@dataclass
class Share:
    requests: int = 0
    dispatched: int = 0
    served_cost: int = 0
    waits: list[float] = field(default_factory=list)
    errors: Counter[str] = field(default_factory=Counter)

    @property
    def wait_p50(self) -> float:
        return percentile(self.waits, 0.5)

    @property
    def wait_p95(self) -> float:
        return percentile(self.waits, 0.95)


@dataclass
class Report:
    accounts: dict[str, Share]
    roots: dict[str, Share]
    tasks: list[TaskResult]

    @property
    def on_time_ratio(self) -> float:
        return sum(t.on_time for t in self.tasks) / len(self.tasks) if self.tasks else 0.0

    def jain_accounts(self, measure: str = "served_cost") -> float:
        return jain([float(getattr(s, measure)) for s in self.accounts.values()])

    def render(self) -> str:
        lines = [
            f"{'account':<12}{'reqs':>6}{'disp':>6}{'cost':>10}{'wait50':>9}{'wait95':>9}  errors",
        ]
        for name, share in sorted(self.accounts.items()):
            lines.append(
                f"{name:<12}{share.requests:>6}{share.dispatched:>6}{share.served_cost:>10}"
                f"{share.wait_p50:>9.2f}{share.wait_p95:>9.2f}  {dict(share.errors)}"
            )
        kinds: dict[str, list[TaskResult]] = defaultdict(list)
        for task in self.tasks:
            kinds[task.spec.kind].append(task)
        lines.append("")
        for kind, items in sorted(kinds.items()):
            done = [t.finished - t.started for t in items if t.ok and t.finished is not None]
            lines.append(
                f"{kind:<12} tasks={len(items)} on_time={sum(t.on_time for t in items)} "
                f"failed={sum(not t.ok for t in items)} p95={percentile(done, 0.95):.1f}s"
            )
        lines.append(
            f"on_time_ratio={self.on_time_ratio:.3f} "
            f"jain(cost)={self.jain_accounts():.3f} jain(dispatched)={self.jain_accounts('dispatched'):.3f}"
        )
        return "\n".join(lines)


def summarize(records: list[GatewayRecord], tasks: list[TaskResult]) -> Report:
    accounts: dict[str, Share] = defaultdict(Share)
    roots: dict[str, Share] = defaultdict(Share)
    for record in records:
        for share in (accounts[record.tenant_id], roots[record.request.root_task_id]):
            share.requests += 1
            share.served_cost += record.consumed_units
            created, dispatched = _ts(record.created_at), _ts(record.dispatched_at)
            if dispatched is not None and created is not None:
                share.dispatched += 1
                share.waits.append(dispatched - created)
            if record.error:
                share.errors[record.error.split(":")[0]] += 1
    return Report(dict(accounts), dict(roots), tasks)
