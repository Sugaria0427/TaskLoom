"""L4 behaviour under faked clients and pool; known defects are strict xfails."""

import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

import pytest
from l4kit import (
    EPOCH,
    AccountSpec,
    Behavior,
    MemoryRepository,
    PoolRejected,
    Scenario,
    ScriptedPool,
    SimulatedClient,
    TaskSpec,
    Workload,
    history,
    mixed_workload,
    run_scenario,
    run_virtual,
)

from taskloom.contracts.models import (
    AccountRecord,
    ModelReply,
    ModelRequest,
    PlatformConfig,
    PoolAccount,
    RootRegistration,
)
from taskloom.domain.model import DomainError, Outcome
from taskloom.gateway.service import GatewayService

ROOMY = PlatformConfig(
    pool_capacity=2,
    requests_per_window=100000,
    tokens_per_window=10_000_000,
    total_budget_units=100_000_000,
)


def account(name: str, **limits: int) -> AccountSpec:
    defaults = {
        "budget_units": 1_000_000,
        "max_inflight": 2,
        "requests_per_window": 10000,
        "tokens_per_window": 1_000_000,
    }
    return AccountSpec(name, **{**defaults, **limits})


# Outcomes an overloaded but correct platform may legitimately produce.
LOAD_OUTCOMES = {"ACCOUNT_BUDGET", "DEADLINE_EXPIRED", "BUDGET_EXHAUSTED"}


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_overloaded_mixed_load_keeps_ledger_invariants(seed: int) -> None:
    result = run_scenario(Scenario(mixed_workload(seed=seed, users=6, tasks_per_user=4)))
    assert result.violations == []
    assert {t.error for t in result.tasks if not t.ok} <= LOAD_OUTCOMES
    assert result.pool.peak <= result.service.config.pool_capacity
    assert result.service.quota.reserved_units == 0


def test_mixed_load_completes_when_capacity_is_ample() -> None:
    workload = mixed_workload(seed=1, users=6, tasks_per_user=4, account=account("template"))
    config = ROOMY.model_copy(update={"pool_capacity": 12})
    result = run_scenario(Scenario(workload, config))
    assert result.violations == []
    assert all(task.on_time for task in result.tasks), [t.error for t in result.tasks]


@pytest.mark.parametrize("coordinated", [False, True])
def test_round_robin_equalizes_dispatches_not_cost(coordinated: bool) -> None:
    """The rr baseline: count fairness lets long requests take most pool service."""
    workload = Workload(
        (account("heavy"), account("light")),
        (
            TaskSpec(
                "heavy", "heavy-root", branches=4, rounds=30, input_tokens=2000,
                input_growth=0, max_tokens=800, output_tokens=400, think_seconds=0,
                deadline_seconds=3000, budget_units=10_000_000,
            ),
            TaskSpec(
                "light", "light-root", branches=4, rounds=60, input_tokens=200,
                input_growth=0, max_tokens=80, output_tokens=40, think_seconds=0,
                deadline_seconds=3000, budget_units=10_000_000,
            ),
        ),
    )  # fmt: skip
    config = ROOMY.model_copy(update={"scheduling_policy": "rr"})
    result = run_scenario(Scenario(workload, config, coordinated=coordinated))
    assert result.violations == []
    # Compare only the contended period, before either account runs alone.
    cut = min(t.finished for t in result.tasks if t.finished is not None)
    dispatched: Counter[str] = Counter()
    cost: Counter[str] = Counter()
    for record in history(result.service):
        if record.dispatched_at and datetime.fromisoformat(record.dispatched_at).timestamp() < cut:
            dispatched[record.tenant_id] += 1
            cost[record.tenant_id] += record.consumed_units
    assert 0.75 <= dispatched["heavy"] / dispatched["light"] <= 1.25
    assert cost["heavy"] / cost["light"] > 5


def test_occupancy_policy_shares_slot_time_and_gateway_places_accounts() -> None:
    """drr charges slot-seconds, so long requests no longer buy most of the pool."""
    workload = Workload(
        (account("heavy"), account("light")),
        (
            TaskSpec(
                "heavy", "heavy-root", branches=4, rounds=30, input_tokens=2000,
                input_growth=0, max_tokens=800, output_tokens=400, think_seconds=0,
                deadline_seconds=3000, budget_units=10_000_000,
            ),
            TaskSpec(
                "light", "light-root", branches=4, rounds=60, input_tokens=200,
                input_growth=0, max_tokens=80, output_tokens=40, think_seconds=0,
                deadline_seconds=3000, budget_units=10_000_000,
            ),
        ),
    )  # fmt: skip
    config = ROOMY.model_copy(
        update={
            "scheduling_policy": "drr",
            "pool_accounts": [
                PoolAccount(account_id="a0", capacity=1),
                PoolAccount(account_id="a1", capacity=1),
            ],
        }
    )
    pool = ScriptedPool(accounts=(1, 1))
    result = run_scenario(Scenario(workload, config, pool=pool))
    assert result.violations == []
    assert result.service.status()["policy"] == "drr"
    cut = min(t.finished for t in result.tasks if t.finished is not None)
    held: Counter[str] = Counter()
    for record in result.service.requests.values():
        assert record.upstream_account in {"a0", "a1"}
        if record.dispatched_at and record.completed_at:
            start = datetime.fromisoformat(record.dispatched_at).timestamp()
            end = datetime.fromisoformat(record.completed_at).timestamp()
            held[record.tenant_id] += max(0.0, min(end, cut) - start)
    assert 0.7 <= held["heavy"] / held["light"] <= 1.4
    # Each chain keeps to one account while it is free: few moves for eight chains.
    moved = sum(call.moved for call in pool.log)
    assert moved < len(pool.log) / 4


def test_lost_reply_is_reconciled_once_without_replay() -> None:
    lost = "alice-root-b0-r0"
    pool = ScriptedPool(capacity=2)
    pool.plan(lost, Behavior(output_tokens=32, fault="lost"))
    workload = Workload((account("alice"),), (TaskSpec("alice", "alice-root", rounds=1),))
    result = run_scenario(Scenario(workload, ROOMY, pool=pool))
    record = result.service.get("alice", lost)
    assert record.state == "FINISHED"
    assert record.outcome == Outcome.SUCCEEDED
    assert sum(pool.calls.values()) == 1
    assert result.service.quota.consumed_units == record.consumed_units > 0
    assert result.violations == []


def test_queued_request_expires_at_its_deadline() -> None:
    pool = ScriptedPool(capacity=1)
    pool.plan("hog-root-b0-r0", Behavior(service_seconds=100))
    workload = Workload(
        (account("hog"), account("late")),
        (
            TaskSpec("hog", "hog-root", rounds=1, deadline_seconds=300),
            TaskSpec("late", "late-root", start_at=1, rounds=1, deadline_seconds=5),
        ),
    )
    config = ROOMY.model_copy(update={"pool_capacity": 1})
    result = run_scenario(Scenario(workload, config, pool=pool))
    late = result.service.get("late", "late-root-b0-r0")
    assert (late.state, late.error, late.dispatched_at) == ("FINISHED", "DEADLINE_EXPIRED", None)
    assert result.violations == []


def test_cancel_while_queued_finishes_without_reservation() -> None:
    pool = ScriptedPool(capacity=1)
    pool.plan("hog-root-b0-r0", Behavior(service_seconds=100))
    workload = Workload(
        (account("hog"), account("quitter")),
        (
            TaskSpec("hog", "hog-root", rounds=1, deadline_seconds=300),
            TaskSpec("quitter", "quit-root", start_at=1, rounds=1, cancel_after=3),
        ),
    )
    config = ROOMY.model_copy(update={"pool_capacity": 1})
    result = run_scenario(Scenario(workload, config, pool=pool))
    record = result.service.get("quitter", "quit-root-b0-r0")
    assert (record.outcome, record.reserved_units, record.dispatched_at) == (
        Outcome.CANCELLED,
        0,
        None,
    )
    assert next(t for t in result.tasks if t.spec.account_id == "quitter").error == "CANCELLED"
    assert result.violations == []


def test_account_request_window_paces_dispatch() -> None:
    workload = Workload(
        (account("alice", requests_per_window=3, max_inflight=1),),
        (TaskSpec("alice", "alice-root", rounds=9, think_seconds=0, deadline_seconds=300),),
    )
    config = ROOMY.model_copy(update={"window_seconds": 10})
    result = run_scenario(Scenario(workload, config))
    assert result.violations == []
    (task,) = result.tasks
    assert task.ok
    # Nine requests at three per window need two full window resets.
    assert task.finished is not None and task.finished - EPOCH >= 20


def test_rejected_before_execution_releases_reservation() -> None:
    pool = ScriptedPool(capacity=2)
    pool.plan("alice-root-b0-r0", Behavior(fault="reject"))
    workload = Workload((account("alice"),), (TaskSpec("alice", "alice-root", rounds=1),))
    result = run_scenario(Scenario(workload, ROOMY, pool=pool))
    assert result.service.quota.inflight == 0
    assert result.service.quota.reserved_units == 0


@dataclass
class RefusingPool(ScriptedPool):
    """Refuses each request's first `refuse` dispatches before running anything."""

    refuse: int = 2

    async def generate(self, request: ModelRequest, account: str | None = None) -> ModelReply:
        if self.refusals[request.request_id] < self.refuse:
            self.refusals[request.request_id] += 1
            raise PoolRejected("pool busy")
        return await super().generate(request)


def test_rejected_request_backs_off_then_succeeds() -> None:
    pool = RefusingPool(capacity=2, refuse=2)
    workload = Workload((account("alice"),), (TaskSpec("alice", "alice-root", rounds=1),))
    result = run_scenario(Scenario(workload, ROOMY, pool=pool))
    assert result.violations == []
    (task,) = result.tasks
    record = result.service.get("alice", "alice-root-b0-r0")
    assert (record.outcome, record.dispatch_attempts, record.error) == (
        Outcome.SUCCEEDED,
        2,
        None,
    )
    assert list(pool.calls.values()) == [1]
    # Backoff of 1 s, then 2 s, before the third dispatch runs.
    assert task.finished is not None and task.finished - EPOCH >= 3
    assert result.service.quota.reserved_units == 0


def test_repeated_rejection_fails_with_pool_rejected() -> None:
    pool = ScriptedPool(capacity=2)
    pool.plan("alice-root-b0-r0", Behavior(fault="reject"))
    workload = Workload((account("alice"),), (TaskSpec("alice", "alice-root", rounds=1),))
    result = run_scenario(Scenario(workload, ROOMY, pool=pool))
    assert result.violations == []
    record = result.service.get("alice", "alice-root-b0-r0")
    assert (record.state, record.outcome, record.error) == (
        "FINISHED",
        Outcome.FAILED,
        "POOL_REJECTED",
    )
    assert record.dispatch_attempts == GatewayService.max_pool_rejections
    assert sum(pool.refusals.values()) == GatewayService.max_pool_rejections
    assert not pool.calls
    assert result.tasks[0].error == "POOL_REJECTED"


async def _forget_finished(service: GatewayService, pool: ScriptedPool) -> None:
    service.finished_retention_seconds = 0


def test_root_budget_holds_after_finished_records_are_pruned() -> None:
    task = TaskSpec(
        "alice", "alice-root", rounds=40, input_tokens=100, input_growth=0, max_tokens=60,
        output_tokens=60, think_seconds=0, deadline_seconds=600, budget_units=2000,
    )  # fmt: skip
    workload = Workload((account("alice"),), (task,))
    result = run_scenario(Scenario(workload, ROOMY, hook=_forget_finished))
    assert result.violations == []
    records = history(result.service)
    assert len(result.service.requests) < len(records)
    assert result.tasks[0].error == "ROOT_BUDGET_EXHAUSTED"
    assert 0 < sum(r.consumed_units for r in records) <= task.budget_units


def test_duplicate_after_pruning_is_answered_from_the_store() -> None:
    async def main() -> None:
        loop = asyncio.get_running_loop()
        store = MemoryRepository()
        spec = account("alice")
        store.seed_account(
            AccountRecord(
                account_id=spec.account_id, password_hash="seeded$",
                budget_units=spec.budget_units, max_inflight=spec.max_inflight,
                requests_per_window=spec.requests_per_window,
                tokens_per_window=spec.tokens_per_window,
            )
        )  # fmt: skip
        pool = ScriptedPool(capacity=2)
        service = GatewayService(store, pool, ROOMY, clock=loop.wall)
        service.finished_retention_seconds = 0
        await service.start()
        try:
            task = TaskSpec("alice", "alice-root", rounds=1)
            client = SimulatedClient(service, pool, loop.wall, loop.wall())
            await service.register_root("alice", RootRegistration(root_task_id=task.root_task_id))
            request = client.request(task, 0, 0)
            await service.submit("alice", request)
            key = service.key("alice", request.request_id)
            while key in service.requests:  # noqa: ASYNC110
                await asyncio.sleep(0.1)
            finished = await service.query("alice", request.request_id)
            assert finished.outcome == Outcome.SUCCEEDED
            assert await service.submit("alice", request) == finished
            assert await service.cancel("alice", request.request_id) == finished
            changed = request.model_copy(update={"max_tokens": request.max_tokens + 1})
            with pytest.raises(DomainError) as error:
                await service.submit("alice", changed)
            assert error.value.code == "DUPLICATE_ID_CONFLICT"
            await asyncio.sleep(1)
            assert list(pool.calls.values()) == [1]
            assert service.quota.consumed_units == finished.consumed_units > 0
            assert key not in service.requests
        finally:
            await service.close()

    run_virtual(main)


def test_platform_keeps_serving_past_1024_requests() -> None:
    workload = Workload(
        (account("alice", budget_units=10_000_000),),
        (
            TaskSpec(
                "alice", "alice-root", rounds=1100, input_tokens=20, input_growth=0,
                max_tokens=8, output_tokens=4, think_seconds=0, deadline_seconds=600,
                budget_units=10_000_000,
            ),
        ),
    )  # fmt: skip
    result = run_scenario(Scenario(workload, ROOMY))
    assert result.tasks[0].ok, result.tasks[0].rejections[-1:]


def test_gateway_uses_the_injected_clock_for_records() -> None:
    result = run_scenario(
        Scenario(Workload((account("alice"),), (TaskSpec("alice", "alice-root", rounds=1),)))
    )
    record = result.service.get("alice", "alice-root-b0-r0")
    assert isinstance(result.service, GatewayService)
    assert datetime.fromisoformat(record.created_at).timestamp() - EPOCH < 1
