"""Two-number reservations: worst cases admit budgets, estimates are reported and windowed."""

import asyncio

from l4kit import (
    AccountSpec,
    Behavior,
    Chain,
    MemoryRepository,
    Scenario,
    ScriptedPool,
    Step,
    TaskSpec,
    VirtualEventLoop,
    Workload,
    iso,
    run_scenario,
    run_virtual,
    sized_messages,
)

from taskloom.contracts.models import (
    AccountRecord,
    ModelRequest,
    PlatformConfig,
    PlatformFeedback,
    RootRegistration,
)
from taskloom.gateway.service import GatewayService

PRICED = PlatformConfig(
    pool_capacity=4,
    requests_per_window=100000,
    tokens_per_window=10_000_000,
    total_budget_units=100_000_000,
    output_price=5.0,
)


def account(name: str, **limits: int) -> AccountSpec:
    defaults = {
        "budget_units": 1_000_000,
        "max_inflight": 4,
        "requests_per_window": 10000,
        "tokens_per_window": 1_000_000,
    }
    return AccountSpec(name, **{**defaults, **limits})


def bursting_chains(budget: int) -> Workload:
    """Four chains whose outputs are tiny for three rounds, then hit max_tokens at 5x price."""
    small = Step(100, 5, cost_units=100 + 5 * 5, service_seconds=1)
    large = Step(100, 200, cost_units=100 + 200 * 5, service_seconds=5)
    steps = (small,) * 3 + (large,) * 3
    task = TaskSpec(
        "alice", "alice-root", max_tokens=200, deadline_seconds=600, budget_units=10_000_000,
        chains=tuple(Chain(0.0, steps) for _ in range(4)),
    )  # fmt: skip
    return Workload((account("alice", budget_units=budget),), (task,))


def spent(service: GatewayService, tenant: str) -> int:
    return sum(r.consumed_units for r in service.requests.values() if r.tenant_id == tenant)


def test_budget_is_never_overrun_when_output_costs_more_than_estimated() -> None:
    result = run_scenario(Scenario(bursting_chains(budget=4000), PRICED))
    assert result.violations == []
    assert spent(result.service, "alice") <= 4000
    # The estimate learned the tiny outputs, so it under-reserved the bursts...
    large = [r for r in result.service.requests.values() if r.consumed_units > 1000]
    held = result.store.reservations
    assert any(held[result.service.key("alice", r.request.request_id)] < 1000 for r in large)
    # ...and the budget held only because admission summed the worst cases.
    assert all(not t.ok for t in result.tasks)


def test_a_price_blind_platform_overruns_the_same_budget() -> None:
    blind = PRICED.model_copy(update={"output_price": 1.0})
    result = run_scenario(Scenario(bursting_chains(budget=4000), blind))
    assert spent(result.service, "alice") > 4000
    assert any(v.endswith("alice budget exceeded") for v in result.violations)


def test_token_windows_true_up_to_actual_usage() -> None:
    pool = ScriptedPool(capacity=2)
    steps = (Step(100, 10, service_seconds=1), Step(150, 20, service_seconds=1))
    task = TaskSpec("alice", "alice-root", max_tokens=200, chains=(Chain(0.0, steps),))
    workload = Workload((account("alice"),), (task,))
    result = run_scenario(Scenario(workload, PRICED, pool=pool))
    assert result.violations == []
    records = sorted(result.service.requests.values(), key=lambda r: r.dispatched_at or "")
    actual = [r.reply.usage.input_tokens + r.reply.usage.output_tokens for r in records if r.reply]
    # The first request had no history and was charged its max_tokens; usage replaced it.
    assert records[0].window_tokens > actual[0]
    assert result.service.account_quotas["alice"].window_tokens == sum(actual)
    assert result.service.quota.window_tokens == sum(actual)


def test_a_rolled_over_window_keeps_its_charge() -> None:
    steps = (Step(100, 10, service_seconds=15),)
    task = TaskSpec("alice", "alice-root", max_tokens=200, chains=(Chain(0.0, steps),))
    config = PRICED.model_copy(update={"window_seconds": 10})
    result = run_scenario(Scenario(Workload((account("alice"),), (task,)), config))
    assert result.violations == []
    # Dispatched into the first window and finished in the second, which never saw it.
    assert result.service.account_quotas["alice"].window_tokens == 0


async def _feedback_probe() -> tuple[list[PlatformFeedback], int]:
    loop = asyncio.get_running_loop()
    assert isinstance(loop, VirtualEventLoop)
    store = MemoryRepository()
    store.seed_account(
        AccountRecord(
            account_id="alice", password_hash="seeded$", budget_units=10_000,
            max_inflight=1, requests_per_window=2, tokens_per_window=1_000_000,
        )
    )  # fmt: skip
    pool = ScriptedPool(capacity=2)
    config = PRICED.model_copy(update={"window_seconds": 60})
    service = GatewayService(store, pool, config, clock=loop.wall)
    await service.start()
    seen = [await service.feedback("alice")]
    await service.register_root("alice", RootRegistration(root_task_id="root"))

    def request(name: str, tokens: int = 100, max_tokens: int = 200) -> ModelRequest:
        return ModelRequest(
            request_id=name, root_task_id="root", agent_id="agent", step_id=name,
            attempt_id=name, messages=sized_messages(tokens), max_tokens=max_tokens,
            deadline_at=iso(loop.wall() + 600),
        )  # fmt: skip

    for name in ("one", "two", "big"):
        pool.plan(name, Behavior(output_tokens=10, service_seconds=20))
    first = request("one")
    await service.submit("alice", first)
    await asyncio.sleep(1)
    await service.submit("alice", request("two"))  # queued behind max_inflight
    await asyncio.sleep(1)
    seen.append(await service.feedback("alice"))
    await asyncio.sleep(25)  # one finishes, two dispatches: two requests fill the window
    seen.append(await service.feedback("alice"))
    await asyncio.sleep(40)  # window rolled over, both finished
    # A request whose worst case takes exactly what the budget has left.
    left = 10_000 - service.quota.consumed_units
    tokens = next(t for t in range(100, 110) if (left - request("big", t).input_tokens) % 5 == 0)
    big = request("big", tokens, (left - request("big", tokens).input_tokens) // 5)
    assert service.worst(big) == left
    await service.submit("alice", big)
    await asyncio.sleep(1)
    seen.append(await service.feedback("alice"))
    await service.close()
    return seen, service.worst(first)


def test_feedback_reports_why_sending_waits() -> None:
    (idle, queued, windowed, budget), worst = run_virtual(_feedback_probe)
    assert (idle.can_submit, idle.blocked_by, idle.account_admission_headroom_units) == (
        True, None, 10_000,
    )  # fmt: skip
    assert idle.output_price == 5.0 and idle.expected_output_tokens is None
    assert (queued.can_submit, queued.blocked_by) == (False, "queue")
    assert queued.account_inflight_worst_units == worst > 1000
    assert queued.account_admission_headroom_units == 10_000 - worst
    assert (windowed.can_submit, windowed.blocked_by) == (False, "window")
    # Retry when the account's own window rolls over: it opened at the first dispatch.
    assert 60 - 28 - 1 <= windowed.retry_after_seconds <= 60 - 26 + 1
    assert windowed.expected_output_tokens == 15
    assert (budget.can_submit, budget.blocked_by) == (False, "budget")
    assert budget.account_admission_headroom_units == 0
