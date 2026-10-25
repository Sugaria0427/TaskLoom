"""Closed-loop stand-in for personal L3 clients, calling GatewayService in process.

It follows the real client's contract (ClientService.model / wait_platform): register the
root, optionally wait on account feedback, submit, poll every 50 ms, think, repeat.
"""

import asyncio
import contextlib
import json
import random
from collections.abc import Callable
from dataclasses import dataclass, field

from taskloom.contracts.models import GatewayRecord, ModelRequest, RootRegistration
from taskloom.domain.model import DomainError, Outcome
from taskloom.gateway.service import GatewayService

from .loop import iso
from .pool import Behavior, ScriptedPool
from .workload import Step, TaskSpec

POLL_SECONDS = 0.05
FEEDBACK_INTERVAL_SECONDS = 1.0
RETRY_SECONDS = 1.0

_EMPTY = len(json.dumps({"messages": [{"role": "user", "content": ""}], "tools": []}))


def sized_messages(tokens: int) -> list[dict[str, str]]:
    """User message whose mock token count matches `tokens` within one unit."""
    return [{"role": "user", "content": "x" * max(0, tokens * 4 - _EMPTY)}]


@dataclass
class TaskResult:
    spec: TaskSpec
    started: float
    finished: float | None = None
    error: str | None = None
    requests: list[str] = field(default_factory=list)
    rejections: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None and self.finished is not None

    @property
    def on_time(self) -> bool:
        return (
            self.ok
            and self.finished is not None
            and (self.finished - self.started <= self.spec.deadline_seconds)
        )


class SimulatedClient:
    def __init__(
        self,
        service: GatewayService,
        pool: ScriptedPool,
        clock: Callable[[], float],
        origin: float,
        coordinated: bool = True,
        duplicate_rate: float = 0.0,
        seed: int = 0,
    ) -> None:
        self.service = service
        self.pool = pool
        self.clock = clock
        self.origin = origin
        self.coordinated = coordinated
        # Share of accepted requests transmitted a second time, as a lost HTTP response would.
        self.duplicate_rate = duplicate_rate
        self.seed = seed
        # When each request became ready to send, and when the client stopped wanting it
        # without a completed reply; keyed by (account, request_id).
        self.ready: dict[tuple[str, str], float] = {}
        self.abandoned: dict[tuple[str, str], float] = {}

    def request(
        self, spec: TaskSpec, branch: int, round_index: int, input_tokens: int | None = None
    ) -> ModelRequest:
        if input_tokens is None:
            input_tokens = spec.request_input_tokens(round_index)
        name = f"{spec.root_task_id}-b{branch}-r{round_index}"
        return ModelRequest(
            request_id=name,
            root_task_id=spec.root_task_id,
            agent_id=f"{spec.root_task_id}-b{branch}",
            step_id=name,
            attempt_id=name,
            messages=sized_messages(input_tokens),
            max_tokens=spec.max_tokens,
            deadline_at=iso(self.deadline(spec)),
        )

    def deadline(self, spec: TaskSpec) -> float:
        return self.origin + spec.start_at + spec.deadline_seconds

    async def run_task(self, spec: TaskSpec) -> TaskResult:
        await asyncio.sleep(max(0, self.origin + spec.start_at - self.clock()))
        result = TaskResult(spec, self.clock())
        tenant = spec.account_id
        try:
            await self.service.register_root(
                tenant,
                RootRegistration(root_task_id=spec.root_task_id, budget_units=spec.budget_units),
            )
            if spec.cancel_after is None:
                await self._branches(spec, result)
            else:
                try:
                    async with asyncio.timeout(spec.cancel_after):
                        await self._branches(spec, result)
                except TimeoutError:
                    result.error = "CANCELLED"
            if result.error is None:
                result.finished = self.clock()
        except* DomainError as group:
            result.error = result.error or getattr(group.exceptions[0], "code", "ERROR")
        return result

    async def _branches(self, spec: TaskSpec, result: TaskResult) -> None:
        # A failed chain fails its task, so the client cancels the sibling chains too.
        async with asyncio.TaskGroup() as group:
            for branch in range(spec.chain_count):
                group.create_task(self._chain(spec, branch, result))

    def draw(self, tag: str, tenant: str, request_id: str) -> float:
        return random.Random(f"{self.seed}:{tag}:{tenant}:{request_id}").random()

    async def _chain(self, spec: TaskSpec, branch: int, result: TaskResult) -> None:
        if spec.chains:
            await asyncio.sleep(spec.chains[branch].offset)
        previous: Step | None = None
        for round_index, step in enumerate(spec.steps(branch)):
            if round_index or spec.chains:
                await asyncio.sleep(step.think_seconds)
            request = self.request(spec, branch, round_index, step.input_tokens)
            # Faults planned by a scenario take precedence over the workload's output size.
            self.pool.behaviors.setdefault(request.attempt_id, self._behavior(step, previous))
            previous = step
            result.requests.append(request.request_id)
            tenant = spec.account_id
            key = (tenant, request.request_id)
            self.ready[key] = self.clock()
            try:
                record = await self._submit(spec, request, result)
                if self.draw("duplicate", tenant, request.request_id) < self.duplicate_rate:
                    with contextlib.suppress(DomainError):
                        await self.service.submit(tenant, request)
                record = await self._settle(tenant, record)
            except asyncio.CancelledError:
                self.abandoned.setdefault(key, self.clock())
                with contextlib.suppress(DomainError):
                    await self.service.cancel(tenant, request.request_id)
                raise
            except DomainError:
                self.abandoned.setdefault(key, self.clock())
                raise
            if record.outcome != Outcome.SUCCEEDED:
                self.abandoned.setdefault(key, self.clock())
                raise DomainError(record.error or "MODEL_" + record.outcome, record.state)
        if not spec.chains:
            await asyncio.sleep(spec.think_seconds)

    @staticmethod
    def _behavior(step: Step, previous: Step | None) -> Behavior:
        gap = (
            (previous.service_seconds or 0.0) + step.think_seconds if previous is not None else None
        )
        return Behavior(
            output_tokens=step.output_tokens,
            service_seconds=step.service_seconds,
            cost_units=step.cost_units,
            cached_tokens=step.cached_tokens,
            cache_discount=step.cache_discount,
            cold_seconds=step.cold_seconds,
            trace_gap=gap,
        )

    async def _submit(
        self, spec: TaskSpec, request: ModelRequest, result: TaskResult
    ) -> GatewayRecord:
        while True:
            if self.coordinated:
                await self._wait_feedback(spec, request)
            try:
                return await self.service.submit(spec.account_id, request)
            except DomainError as error:
                result.rejections.append(error.code)
                if not error.retryable or self.clock() >= self.deadline(spec):
                    raise
                # Retransmission reuses the request identity, as the real client does.
                await asyncio.sleep(RETRY_SECONDS)

    async def _wait_feedback(self, spec: TaskSpec, request: ModelRequest) -> None:
        while True:
            feedback = await self.service.feedback(spec.account_id)
            worst, tokens = feedback.admission_needs(request)
            if feedback.account_budget_units - feedback.account_consumed_units < worst:
                raise DomainError("ACCOUNT_BUDGET", "Platform account budget exhausted")
            if (
                feedback.can_submit
                and feedback.account_admission_headroom_units >= worst
                and feedback.account_window_tokens_remaining >= tokens
            ):
                return
            if self.clock() >= self.deadline(spec):
                raise DomainError("DEADLINE_EXPIRED", "Deadline expired while waiting")
            await asyncio.sleep(
                max(0.1, min(feedback.retry_after_seconds, FEEDBACK_INTERVAL_SECONDS))
            )

    async def _settle(self, tenant: str, record: GatewayRecord) -> GatewayRecord:
        # Mirrors the real client: poll the observable record, stop at FINISHED or UNKNOWN.
        while record.state not in ("FINISHED", "UNKNOWN"):  # noqa: ASYNC110
            await asyncio.sleep(POLL_SECONDS)
            record = await self.service.query(tenant, record.request.request_id)
        return record
