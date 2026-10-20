"""Ledger invariants that must hold at every observable point of the platform."""

from collections import defaultdict

from taskloom.contracts.models import GatewayRecord, QuotaState
from taskloom.gateway.service import GatewayService

from .pool import ScriptedPool
from .store import MemoryRepository

HOLDING = ("DISPATCH_INTENT", "UNKNOWN")
# Messages with this prefix report a window or budget overrun: a measured shortfall of the
# reservation estimate, not an inconsistent ledger.
LIMIT = "limit: "


def history(service: GatewayService) -> list[GatewayRecord]:
    """Every request the service ever accepted, including finished ones pruned from memory."""
    if isinstance(service.store, MemoryRepository):
        return list(service.store.requests.values())
    return list(service.requests.values())


def window_allowance(records: list[GatewayRecord], quota: QuotaState) -> int:
    """How far finish-time true-ups may push a token window past its limit.

    A window admits on estimated tokens and trues each request up to its actual usage when it
    finishes in the window it was dispatched into, so only upward true-ups can overshoot.
    """
    return sum(
        max(0, r.reply.usage.input_tokens + r.reply.usage.output_tokens - r.window_tokens)
        for r in records
        if r.reply is not None and GatewayService._in_window(r, quota)
    )


def ledger_violations(service: GatewayService, pool: ScriptedPool | None = None) -> list[str]:
    found: list[str] = []
    records = history(service)
    config = service.config

    def check(condition: bool, message: str) -> None:
        if not condition:
            found.append(message)

    for record in records:
        name = f"{record.tenant_id}:{record.request.request_id}"
        holds = record.state in HOLDING
        check(
            holds == (record.reserved_units > 0),
            f"{name} state {record.state} reserves {record.reserved_units}",
        )
        check(
            record.worst_units >= record.reserved_units if holds else record.worst_units == 0,
            f"{name} state {record.state} holds worst case {record.worst_units}",
        )
        if record.state == "FINISHED":
            check(record.quiescent, f"{name} finished but not quiescent")

    # Sums run over the full history in the store, since memory holds only recent records.
    quota = service.quota
    check(
        quota.reserved_units == sum(r.reserved_units for r in records),
        "global reserved != sum of records",
    )
    check(
        quota.worst_units == sum(r.worst_units for r in records),
        "global worst case != sum of records",
    )
    check(
        quota.consumed_units == sum(r.consumed_units for r in records),
        "global consumed != sum of records",
    )
    check(
        quota.inflight == sum(r.state in HOLDING for r in records),
        "global inflight != holding records",
    )
    check(quota.inflight <= config.pool_capacity, "global inflight exceeds pool capacity")
    check(
        quota.window_requests <= config.requests_per_window,
        LIMIT + "global request window exceeded",
    )
    check(
        quota.window_tokens <= config.tokens_per_window + window_allowance(records, quota),
        LIMIT + "global token window exceeded",
    )
    # Budgets admit worst cases, so even every in-flight request at its worst stays within.
    check(
        quota.consumed_units + quota.worst_units <= config.total_budget_units,
        LIMIT + "global budget exceeded",
    )

    by_tenant = defaultdict(list)
    for record in records:
        by_tenant[record.tenant_id].append(record)
    for tenant, personal in service.account_quotas.items():
        account = service.accounts.accounts[tenant]
        mine = by_tenant.get(tenant, [])
        check(
            personal.reserved_units == sum(r.reserved_units for r in mine),
            f"{tenant} reserved != records",
        )
        check(
            personal.worst_units == sum(r.worst_units for r in mine),
            f"{tenant} worst case != records",
        )
        check(
            personal.consumed_units == sum(r.consumed_units for r in mine),
            f"{tenant} consumed != records",
        )
        check(
            personal.inflight == sum(r.state in HOLDING for r in mine),
            f"{tenant} inflight != records",
        )
        check(personal.inflight <= account.max_inflight, f"{tenant} inflight exceeds account limit")
        check(
            personal.window_requests <= account.requests_per_window,
            f"{LIMIT}{tenant} request window exceeded",
        )
        check(
            personal.window_tokens <= account.tokens_per_window + window_allowance(mine, personal),
            f"{LIMIT}{tenant} token window exceeded",
        )
        check(
            personal.consumed_units + personal.worst_units <= account.budget_units,
            f"{LIMIT}{tenant} budget exceeded",
        )

    if pool is not None:
        replayed = [key for key, count in pool.calls.items() if count > 1]
        check(not replayed, f"pool requests replayed: {replayed[:5]}")
        check(pool.peak <= pool.capacity, "pool peak exceeds pool capacity")
        # Exactly-once billing: every charge matches one execution of that request.
        billed = {call.pool_request_id: call.cost_units for call in pool.log}
        for record in records:
            if record.consumed_units:
                pool_id = service.pool_request_id(record)
                check(
                    billed.get(pool_id) == record.consumed_units,
                    f"{record.tenant_id}:{record.request.request_id} charged "
                    f"{record.consumed_units}, pool billed {billed.get(pool_id)}",
                )
    return found
