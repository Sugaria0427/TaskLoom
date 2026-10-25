"""Exact time-integrated service metrics from per-request lifetimes.

Demand is client-side: a request is wanted from the moment its chain is ready to send it
(after thinking) until its reply completes or the client gives up, wherever it waits, so
coordinated clients that hold requests locally are measured like those queueing at the
platform. Allocation is the platform's in-flight set. Between consecutive events both are
constant, so every metric is an exact integral, with no sampling interval to tune.

Fairness reference: the max-min fair split (water-filling) of the slots in use over the
current demands. An account running one chain wants one slot and is not shortchanged by
holding one while others hold three; it is shortchanged when it waits while they do.
"""

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from l4kit import EPOCH, Result, history


def _ts(value: str | None) -> float | None:
    return datetime.fromisoformat(value).timestamp() if value else None


@dataclass(frozen=True)
class Life:
    account: str
    root: str
    request_id: str
    ready: float
    dispatched: float | None
    released: float | None  # platform stopped holding a slot for it
    wanted_until: float  # demand ends: completed, expired, cancelled or abandoned

    @property
    def wait(self) -> float:
        return (self.dispatched if self.dispatched is not None else self.wanted_until) - self.ready


def lives(result: Result) -> list[Life]:
    end = result.origin + result.elapsed
    records = {(r.tenant_id, r.request.request_id): r for r in history(result.service)}
    found = []
    for key, ready in result.client.ready.items():
        record = records.get(key)
        abandoned = result.client.abandoned.get(key, end)
        dispatched = _ts(record.dispatched_at) if record else None
        completed = _ts(record.completed_at) if record else None
        if dispatched is not None and 0 < dispatched - ready < 1e-6:
            # Record timestamps have microsecond resolution; rounding is not a wait.
            dispatched = ready
        if dispatched is not None:
            released = completed if completed is not None else end
            # A client that gave up (cancelled, unknown outcome) no longer wants the slot,
            # even while the platform still holds it.
            wanted = min(released, abandoned)
        else:
            released = None
            wanted = min(abandoned, completed if completed is not None else end)
        root = record.request.root_task_id if record else key[1].rsplit("-b", 1)[0]
        found.append(Life(key[0], root, key[1], ready, dispatched, released, max(ready, wanted)))
    return found


def waterfill(demands: dict[str, int], capacity: float) -> dict[str, float]:
    """Max-min fair split of `capacity` among `demands`."""
    share: dict[str, float] = {}
    left = capacity
    pending = sorted(demands.items(), key=lambda item: item[1])
    while pending:
        even = left / len(pending)
        name, demand = pending[0]
        if demand <= even:
            share[name] = float(demand)
            left -= demand
            pending.pop(0)
        else:
            for name, _ in pending:
                share[name] = even
            break
    return share


def _deviation(demand: dict[str, int], held: dict[str, int]) -> float | None:
    """Share of the slots in use that sit with the wrong party under max-min fairness."""
    used = sum(held.values())
    if used == 0:
        return None
    fair = waterfill(demand, used)
    return sum(abs(held.get(n, 0) - fair.get(n, 0.0)) for n in demand.keys() | held.keys()) / (
        2 * used
    )


def _shortfall(demand: dict[str, int], held: dict[str, int]) -> float | None:
    """Like _deviation, but slots are whole: a party is shortchanged only below the floor of
    its max-min share. Two parties wanting one slot each with one slot in use are fair here
    (one of them must wait), while _deviation counts half the slot as misallocated."""
    used = sum(held.values())
    if used == 0:
        return None
    fair = waterfill(demand, used)
    return sum(max(0, math.floor(f + 1e-9) - held.get(n, 0)) for n, f in fair.items()) / used


@dataclass(frozen=True)
class Integrals:
    account_unfairness: float  # time-averaged over moments when someone waits
    root_unfairness: float  # within accounts, weighted by the account's slots
    account_shortfall: float  # the same two, counting whole slots (diagnostic, unscored)
    root_shortfall: float
    waste: float  # wanted slot-time the pool spent idle
    contended_seconds: float


def integrate(result: Result, found: list[Life]) -> Integrals:
    capacity = result.pool.capacity
    events: list[tuple[float, int, str, str, str]] = []
    for life in found:
        events.append((life.ready, +1, "demand", life.account, life.root))
        events.append((life.wanted_until, -1, "demand", life.account, life.root))
        if life.dispatched is not None and life.released is not None:
            events.append((life.dispatched, +1, "held", life.account, life.root))
            events.append((life.released, -1, "held", life.account, life.root))
    end = result.origin + result.elapsed
    for call in result.pool.log:
        events.append((EPOCH + call.started, +1, "busy", "", ""))
        events.append((EPOCH + call.finished, -1, "busy", "", ""))
    for started in result.pool.running.values():
        events.append((EPOCH + started, +1, "busy", "", ""))
        events.append((end, -1, "busy", "", ""))
    events.sort(key=lambda e: (e[0], e[1]))

    demand: dict[str, int] = defaultdict(int)
    held: dict[str, int] = defaultdict(int)
    root_demand: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    root_held: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    busy = 0
    unfair = contended = root_unfair = root_weight = wasted = wanted = 0.0
    short = root_short = 0.0
    previous = events[0][0] if events else 0.0
    for at, delta, kind, account, root in events:
        span = at - previous
        if span > 0:
            total_demand = sum(demand.values())
            total_held = sum(held.values())
            wanted += span * min(capacity, total_demand)
            wasted += span * max(0, min(capacity, total_demand) - busy)
            if total_demand > total_held:
                deviation = _deviation(
                    {n: v for n, v in demand.items() if v}, {n: v for n, v in held.items() if v}
                )
                if deviation is not None:
                    unfair += span * deviation
                    contended += span
                    short += span * (
                        _shortfall(
                            {n: v for n, v in demand.items() if v},
                            {n: v for n, v in held.items() if v},
                        )
                        or 0.0
                    )
            for name, slots in held.items():
                if slots and demand[name] > slots:
                    wants = {r: v for r, v in root_demand[name].items() if v}
                    holds = {r: v for r, v in root_held[name].items() if v}
                    inner = _deviation(wants, holds)
                    if inner is not None:
                        root_unfair += span * slots * inner
                        root_weight += span * slots
                        root_short += span * slots * (_shortfall(wants, holds) or 0.0)
            previous = at
        if kind == "demand":
            demand[account] += delta
            root_demand[account][root] += delta
        elif kind == "held":
            held[account] += delta
            root_held[account][root] += delta
        else:
            busy += delta
    return Integrals(
        account_unfairness=unfair / contended if contended else 0.0,
        root_unfairness=root_unfair / root_weight if root_weight else 0.0,
        account_shortfall=short / contended if contended else 0.0,
        root_shortfall=root_short / root_weight if root_weight else 0.0,
        waste=wasted / wanted if wanted else 0.0,
        contended_seconds=contended,
    )
