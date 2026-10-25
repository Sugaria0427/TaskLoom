"""Raw measurements of one run. Scores are derived from these in scoring.py."""

from collections import Counter, defaultdict
from typing import Any

from l4kit import Result, history, percentile
from l4kit.ledger import LIMIT

from taskloom.contracts.models import GatewayRecord

from .calibrate import ideal_seconds
from .facts import STARVATION_WAIT
from .timeline import integrate, lives

# Task service objectives by slowdown (completion time over the time on an idle platform):
# within 2x the request waited no longer than its own work, the bar for an interactive agent;
# within 5x is tolerable for background work. Failed tasks meet neither.
SLO_TIGHT = 2.0
SLO_LOOSE = 5.0
# Slowdown percentiles count failed tasks at this cap (and cap the rest), so a policy that
# drops tasks cannot look faster than one that finishes them late.
SLOWDOWN_CAP = 20.0


def slo_credit(slowdown: float | None) -> float:
    if slowdown is None:
        return 0.0
    return 1.0 if slowdown <= SLO_TIGHT else 0.5 if slowdown <= SLO_LOOSE else 0.0


def cache_metrics(result: Result) -> dict[str, float]:
    """Prompt-cache outcome of the pool's account placement.

    retention: share of the cached tokens a follow-up call still found on its account,
    weighted by those tokens; a chain's first call is excluded since its cache state is
    unknown. miss_cost: share of all charges paid for the missed part. moves: share of
    follow-up calls placed on another account than the chain's previous call.
    """
    log = result.pool.log
    follow = [c for c in log if not c.first]
    cached = [c for c in follow if c.cached_tokens > 0]
    weight = sum(c.cached_tokens for c in cached)
    cost = sum(c.cost_units for c in log)
    return {
        "cache_retention": (
            sum(c.cached_tokens * c.retained for c in cached) / weight if weight else 1.0
        ),
        "cache_miss_cost": sum(c.extra_units for c in log) / cost if cost else 0.0,
        "account_moves": sum(c.moved for c in follow) / len(follow) if follow else 0.0,
    }


def measure(result: Result) -> dict[str, Any]:
    found = lives(result)
    integrals = integrate(result, found)
    records = history(result.service)

    slowdowns: dict[str, float | None] = {}
    credit_by_account: dict[str, list[float]] = defaultdict(list)
    for task in result.tasks:
        value = (
            (task.finished - task.started) / max(ideal_seconds(task.spec), 1e-6)
            if task.ok and task.finished is not None
            else None
        )
        slowdowns[task.spec.root_task_id] = value
        credit_by_account[task.spec.account_id].append(slo_credit(value))
    account_credit = {a: sum(v) / len(v) for a, v in credit_by_account.items()}
    capped = [SLOWDOWN_CAP if v is None else min(v, SLOWDOWN_CAP) for v in slowdowns.values()]

    waits = [life.wait for life in found]
    finished = [r for r in records if r.state == "FINISHED" and r.consumed_units]
    held = result.store.reservations

    def reservation(record: GatewayRecord) -> int:
        return held.get(result.service.key(record.tenant_id, record.request.request_id), 0)

    # The reservation is the gateway's estimate; budgets admit on worst cases, so overrun here
    # means the estimate ran low, and budget_overrun measures whether any budget was exceeded.
    reserved = sum(reservation(r) for r in finished)
    consumed = sum(r.consumed_units for r in finished)
    overrun = sum(max(0, r.consumed_units - reservation(r)) for r in finished)
    budgets = {a.account_id: a.budget_units for a in result.service.accounts.accounts.values()}
    spent: Counter[str] = Counter()
    for record in records:
        spent[record.tenant_id] += record.consumed_units

    consistency = [v for v in result.violations if not v.startswith(LIMIT)]
    return {
        "consistent": not consistency,
        "consistency_violations": consistency[:5],
        "limit_violations": [v for v in result.violations if v.startswith(LIMIT)][:5],
        "requests": len(found),
        "tasks": len(result.tasks),
        "completed": sum(t.ok for t in result.tasks) / max(1, len(result.tasks)),
        "slo_attainment": sum(account_credit.values()) / max(1, len(account_credit)),
        "slo_by_account": account_credit,
        "slowdown_p50": percentile(capped, 0.5),
        "slowdown_p95": percentile(capped, 0.95),
        "slowdown_by_root": slowdowns,
        "wait_p50": percentile(waits, 0.5),
        "wait_p95": percentile(waits, 0.95),
        "wait_max": max(waits, default=0.0),
        "starved": sum(w > STARVATION_WAIT for w in waits) / max(1, len(waits)),
        "account_unfairness": integrals.account_unfairness,
        "root_unfairness": integrals.root_unfairness,
        "account_shortfall": integrals.account_shortfall,
        "root_shortfall": integrals.root_shortfall,
        "contended_seconds": integrals.contended_seconds,
        "waste": integrals.waste,
        "reservation_precision": consumed / reserved if reserved else 1.0,
        "overrun": overrun / consumed if consumed else 0.0,
        "budget_overrun": max(
            (max(0, spent[a] - b) / b for a, b in budgets.items() if b), default=0.0
        ),
        **cache_metrics(result),
        "leaked_inflight": result.service.quota.inflight,
        "leaked_reserved": result.service.quota.reserved_units,
        "pool_capacity": result.pool.capacity,
        "errors": dict(Counter(t.error for t in result.tasks if t.error)),
        "rejections": dict(Counter(code for t in result.tasks for code in t.rejections)),
        "virtual_seconds": result.elapsed,
    }
