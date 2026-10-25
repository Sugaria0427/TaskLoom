"""Run a Workload against the real GatewayService on virtual time, with every neighbour faked."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from taskloom.contracts.models import AccountRecord, PlatformConfig
from taskloom.gateway.service import GatewayService

from .client import SimulatedClient, TaskResult
from .ledger import history, ledger_violations
from .loop import VirtualEventLoop, run_virtual
from .metrics import Report, summarize
from .pool import ScriptedPool
from .store import MemoryRepository
from .workload import Workload


@dataclass
class Scenario:
    workload: Workload
    config: PlatformConfig = field(default_factory=PlatformConfig)
    pool: ScriptedPool | None = None
    coordinated: bool = True
    horizon_seconds: float = 3600
    check_every: float = 0.5
    duplicate_rate: float = 0.0
    seed: int = 0
    # Runs inside the loop after start, e.g. to plan faults or poke the service mid-run.
    hook: Callable[[GatewayService, ScriptedPool], Awaitable[None]] | None = None


@dataclass
class Result:
    report: Report
    violations: list[str]
    service: GatewayService
    pool: ScriptedPool
    store: MemoryRepository
    tasks: list[TaskResult]
    elapsed: float
    client: SimulatedClient
    origin: float
    # Request states when the workload ended, before shutdown cancels in-flight dispatches.
    final_states: dict[str, str]


def run_scenario(scenario: Scenario) -> Result:
    return run_virtual(lambda: _run(scenario))


async def _run(scenario: Scenario) -> Result:
    loop = asyncio.get_running_loop()
    assert isinstance(loop, VirtualEventLoop)
    origin = loop.wall()
    store = MemoryRepository()
    for spec in scenario.workload.accounts:
        store.seed_account(
            AccountRecord(
                account_id=spec.account_id,
                password_hash="seeded$",
                budget_units=spec.budget_units,
                max_inflight=spec.max_inflight,
                requests_per_window=spec.requests_per_window,
                tokens_per_window=spec.tokens_per_window,
            )
        )
    pool = scenario.pool or ScriptedPool(capacity=scenario.config.pool_capacity)
    service = GatewayService(store, pool, scenario.config, clock=loop.wall)
    await service.start()
    violations: list[str] = []

    def observe() -> None:
        for message in ledger_violations(service, pool):
            if message not in violations:
                violations.append(message)

    async def monitor() -> None:
        while True:
            await asyncio.sleep(scenario.check_every)
            async with service.lock:
                observe()

    watcher = asyncio.create_task(monitor())
    client = SimulatedClient(
        service,
        pool,
        loop.wall,
        origin,
        scenario.coordinated,
        scenario.duplicate_rate,
        scenario.seed,
    )
    try:
        if scenario.hook is not None:
            await scenario.hook(service, pool)
        running = [asyncio.create_task(client.run_task(t)) for t in scenario.workload.tasks]
        _, pending = await asyncio.wait(running, timeout=scenario.horizon_seconds)
        # A stalled platform is an outcome to measure: unfinished tasks end at the horizon.
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        tasks = [
            TaskResult(spec, origin + spec.start_at, error="HORIZON")
            if task in pending
            else task.result()
            for spec, task in zip(scenario.workload.tasks, running, strict=True)
        ]
        async with service.lock:
            observe()
            final_states = {key: record.state for key, record in store.requests.items()}
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        await service.close()
    return Result(
        summarize(history(service), list(tasks)),
        violations,
        service,
        pool,
        store,
        list(tasks),
        loop.wall() - origin,
        client,
        origin,
        final_states,
    )
