"""Controlled parallel sub-agents, dependency confluence and cancel/resume recovery.

Issue IKR9I1 acceptance:
- two real branches run in parallel and one summary step conflates them,
  recording the relative order change versus sequential execution;
- branch count never raises the root task budget or resource ceilings;
- a key branch-failure / parent-cancel / restart chain is verified, keeping the
  original identity of unknown executions to avoid double execution and settlement.
"""

import asyncio
import json
import time
from collections.abc import Callable

import pytest
from pydantic import ValidationError

from taskloom.adapters.sqlite import SQLiteRepository
from taskloom.client.service import ClientService
from taskloom.contracts.models import (
    BranchSpec,
    ClientConfig,
    GatewayRecord,
    ModelReply,
    ModelRequest,
    RootRegistration,
    TaskCreate,
    ToolRun,
    ToolState,
    Usage,
    branch_agent_id,
    digest,
)
from taskloom.domain.model import DomainError, Outcome, WorkState


async def standard_model(messages, request):
    """One tool round (file_stats), then a summary consistent with the tool result."""
    last = messages[-1]
    usage = Usage(input_tokens=1, output_tokens=1, cost_units=2)
    if last["role"] == "tool":
        return ModelReply(content='{"files":1,"words":3,"summary":"verified"}', usage=usage)
    return ModelReply(
        tool_calls=[
            {
                "call_id": "stats",
                "name": "file_stats",
                "arguments": {},
            }
        ],
        usage=usage,
    )


class FakeGateway:
    """Programmable gateway: the model reply is a callable of (messages, request)."""

    def __init__(self, feedback, model=standard_model):
        self._feedback = feedback
        self._model = model
        self.submissions: list[ModelRequest] = []
        self.registrations: list[RootRegistration] = []

    async def feedback(self):
        return self._feedback

    async def register(self, root: RootRegistration) -> None:
        self.registrations.append(root)

    async def submit(self, request: ModelRequest) -> GatewayRecord:
        self.submissions.append(request)
        reply = await self._model(request.messages, request)
        return GatewayRecord(
            tenant_id=request.root_task_id,
            request=request,
            payload_hash=digest(request.model_dump(mode="json")),
            state="FINISHED",
            outcome=Outcome.SUCCEEDED,
            quiescent=True,
            consumed_units=2,
            reply=reply,
        )

    async def query(self, request_id: str) -> GatewayRecord:
        return None

    async def cancel(self, request_id: str) -> GatewayRecord:
        return None

    async def close(self) -> None:
        pass


class FakeExecutor:
    """``fail_when`` simulates a transport failure: the run raises and the executor
    loses the attempt (``query`` returns None) until ``healed`` is set, after which
    the original run is confirmed SUCCEEDED instead of being re-executed."""

    def __init__(self, delay: float = 0.0, fail: bool = False):
        self._delay = delay
        self.fail_when: Callable[[ToolRun], bool] = lambda _request: fail
        self.healed = False
        self.lost: set[str] = set()
        self.running: set[str] = set()
        self.runs: list[ToolRun] = []
        self.started_at: list[float] = []
        self.finished_at: list[float] = []
        self.active = 0
        self.peak_active = 0
        self.cancelled: list[str] = []

    def _state(self, request: ToolRun) -> ToolState:
        cancelled = request.attempt_id in self.cancelled
        return ToolState(
            request=request,
            outcome=Outcome.CANCELLED if cancelled else Outcome.SUCCEEDED,
            quiescent=True,
            result=None if cancelled else {"files": 1, "words": 3},
            pid=1,
            start_ticks="1",
            boot_id="1",
            cgroup_path="/sys/fs/cgroup",
        )

    async def run(self, request: ToolRun) -> ToolState:
        self.runs.append(request)
        self.running.add(request.attempt_id)
        self.started_at.append(time.monotonic())
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            if self.fail_when(request):
                self.lost.add(request.attempt_id)
                raise RuntimeError("tool boom")
            return self._state(request)
        finally:
            self.running.discard(request.attempt_id)
            self.active -= 1
            self.finished_at.append(time.monotonic())

    async def query(self, attempt_id: str) -> ToolState | None:
        if attempt_id in self.lost and not self.healed:
            return None
        for request in self.runs:
            if request.attempt_id == attempt_id:
                if attempt_id in self.running:
                    return ToolState(
                        request=request,
                        outcome=Outcome.PENDING,
                        quiescent=False,
                        result=None,
                        pid=1,
                        start_ticks="1",
                        boot_id="1",
                        cgroup_path="/sys/fs/cgroup",
                    )
                return self._state(request)
        return None

    async def cancel(self, attempt_id: str, generation: int) -> bool:
        self.cancelled.append(attempt_id)
        return True

    async def close(self) -> None:
        pass


def owned_by(service: ClientService, agent_id: str) -> Callable[[ToolRun], bool]:
    """Predicate selecting tool runs dispatched by one agent identity."""
    return lambda request: service.attempts[request.attempt_id].agent_id == agent_id


async def runner_finishes(service: ClientService, task_id: str, wait_seconds: float = 15):
    runner = service.runners.get(task_id)
    if runner is None:
        raise AssertionError("No runner for task")
    await asyncio.wait_for(asyncio.shield(runner), wait_seconds)


def two_branch_spec() -> TaskCreate:
    return TaskCreate(
        documents={"notes.txt": "one two three"},
        branches=[
            BranchSpec(agent_id="branch-a", instruction="Count words in part A."),
            BranchSpec(agent_id="branch-b", instruction="Count words in part B."),
        ],
    )


async def started(tmp_path, platform_feedback, gateway, executor, config=None):
    store = SQLiteRepository(tmp_path / "client.db")
    service = ClientService(
        store, gateway, executor, tmp_path, config or ClientConfig(max_inflight=4)
    )
    await service.start()
    return store, service


@pytest.mark.parametrize("delay", [0.0, 0.3])
async def test_two_branches_run_in_parallel_and_parent_conflates(
    tmp_path, platform_feedback, delay
) -> None:
    """Acceptance 1: two actual branches run in parallel; the parent then runs one
    summary step and conflates both branch results."""
    executor = FakeExecutor(delay=delay)
    gateway = FakeGateway(platform_feedback)
    store, service = await started(tmp_path, platform_feedback, gateway, executor)
    try:
        task = await service.create(two_branch_spec())
        # The parent holds no execution permit while branches run: it is WAIT_DEPENDENCY.
        saw_waiting = False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = service.get(task.task_id)
            if current.state == WorkState.WAIT_DEPENDENCY:
                saw_waiting = True
                break
            await asyncio.sleep(0.005)
        await runner_finishes(service, task.task_id)
        current = service.get(task.task_id)
        assert current.outcome == Outcome.SUCCEEDED
        assert current.result is not None and current.result["words"] == 3
        # Both branches reached SUCCEEDED with their own durable result.
        for agent_id in ("branch-a", "branch-b"):
            branch = current.branches[agent_id]
            assert branch.outcome == Outcome.SUCCEEDED
            assert branch.result is not None
            assert branch.result["files"] == 1 and branch.result["words"] == 3
            assert branch.parent_agent_id == current.agent_id
            assert branch.root_task_id == task.task_id
        if delay:
            assert saw_waiting
            # Two branch tools overlapped: peak concurrency reached 2.
            assert executor.peak_active >= 2
            # Parallel is faster than the sequential sum: both 0.3s tools overlap.
            total_span = max(executor.finished_at) - min(executor.started_at)
            sequential_sum = sum(
                end - start
                for start, end in zip(executor.started_at, executor.finished_at, strict=False)
            )
            assert total_span < sequential_sum
        # Every model request stayed on the root task: branches add no budget or share.
        assert {r.root_task_id for r in gateway.submissions} == {task.task_id}
        agent_ids = {r.agent_id for r in gateway.submissions}
        assert agent_ids == {
            current.agent_id,
            branch_agent_id(task.task_id, "branch-a"),
            branch_agent_id(task.task_id, "branch-b"),
        }
        assert current.consumed_units <= current.specification.budget_units
        await store.close()
    finally:
        await service.close()


async def test_branch_dependency_orders_start(tmp_path, platform_feedback) -> None:
    """A branch with depends_on only starts after its dependency SUCCEEDED."""
    executor = FakeExecutor(delay=0.1)
    gateway = FakeGateway(platform_feedback)
    store, service = await started(tmp_path, platform_feedback, gateway, executor)
    try:
        spec = TaskCreate(
            documents={"notes.txt": "one two three"},
            branches=[
                BranchSpec(agent_id="branch-a", instruction="First."),
                BranchSpec(agent_id="branch-b", instruction="Second.", depends_on=["branch-a"]),
            ],
        )
        task = await service.create(spec)
        await runner_finishes(service, task.task_id)
        current = service.get(task.task_id)
        assert current.outcome == Outcome.SUCCEEDED
        assert len(executor.runs) == 3  # branch-a, branch-b, parent summary
        a_start = executor.started_at[0]
        a_end = executor.finished_at[0]
        b_start = executor.started_at[1]
        assert b_start >= a_end
        assert a_start < a_end < b_start
        await store.close()
    finally:
        await service.close()


def is_branch(request: ModelRequest, *names: str) -> bool:
    return any(request.agent_id == branch_agent_id(request.root_task_id, n) for n in names)


async def test_required_branch_failure_fails_parent(tmp_path, platform_feedback) -> None:
    """A failing required branch fails the whole root task. A clean DomainError
    refusal settles the branch attempt, so the root task is FAILED, not UNKNOWN."""
    executor = FakeExecutor(delay=0.05)

    async def failing_model(messages, request):
        if is_branch(request, "branch-a", "branch-b"):
            raise DomainError("MODEL_REJECTED", "branch model refused")
        return await standard_model(messages, request)

    gateway = FakeGateway(platform_feedback, model=failing_model)
    store, service = await started(tmp_path, platform_feedback, gateway, executor)
    try:
        task = await service.create(two_branch_spec())
        await runner_finishes(service, task.task_id)
        current = service.get(task.task_id)
        assert current.outcome == Outcome.FAILED
        assert current.state == WorkState.FINISHED and current.quiescent
        assert "BRANCH_FAILED" in (current.error or "")
        outcomes = [current.branches[name].outcome for name in ("branch-a", "branch-b")]
        # The first failure fails fast; the sibling either failed too or was cancelled.
        assert Outcome.FAILED in outcomes
        assert set(outcomes) <= {Outcome.FAILED, Outcome.CANCELLED}
        await store.close()
    finally:
        await service.close()


async def test_required_branch_failure_cancels_running_siblings(
    tmp_path, platform_feedback
) -> None:
    """Fast failure: once a required branch fails, the root fails without waiting
    for the slow sibling, whose in-flight tool is cancelled."""
    executor = FakeExecutor(delay=5.0)

    async def failing_model(messages, request):
        if is_branch(request, "branch-a"):
            await asyncio.sleep(0.2)
            raise DomainError("MODEL_REJECTED", "branch model refused")
        return await standard_model(messages, request)

    gateway = FakeGateway(platform_feedback, model=failing_model)
    store, service = await started(tmp_path, platform_feedback, gateway, executor)
    try:
        began = time.monotonic()
        task = await service.create(two_branch_spec())
        await runner_finishes(service, task.task_id, 4)
        assert time.monotonic() - began < 4
        current = service.get(task.task_id)
        assert current.outcome == Outcome.FAILED
        assert "BRANCH_FAILED" in (current.error or "")
        assert current.branches["branch-a"].outcome == Outcome.FAILED
        sibling = current.branches["branch-b"]
        assert sibling.outcome == Outcome.CANCELLED
        assert sibling.state == WorkState.FINISHED and sibling.quiescent
        assert len(executor.runs) == 1 and executor.cancelled == [executor.runs[0].attempt_id]
        assert current.quiescent
        await store.close()
    finally:
        await service.close()


async def test_optional_branch_failure_parent_still_succeeds(tmp_path, platform_feedback) -> None:
    """A cleanly failing optional branch does not fail the parent; the parent
    conflates the succeeded branches."""
    executor = FakeExecutor(delay=0.05)

    async def failing_model(messages, request):
        if is_branch(request, "branch-opt"):
            raise DomainError("MODEL_REJECTED", "optional branch rejected")
        return await standard_model(messages, request)

    gateway = FakeGateway(platform_feedback, model=failing_model)
    store, service = await started(tmp_path, platform_feedback, gateway, executor)
    try:
        spec = TaskCreate(
            documents={"notes.txt": "one two three"},
            branches=[
                BranchSpec(agent_id="branch-main", instruction="Main."),
                BranchSpec(agent_id="branch-opt", instruction="Optional.", required=False),
            ],
        )
        task = await service.create(spec)
        await runner_finishes(service, task.task_id)
        current = service.get(task.task_id)
        assert current.outcome == Outcome.SUCCEEDED
        assert current.quiescent
        assert current.branches["branch-main"].outcome == Outcome.SUCCEEDED
        assert current.branches["branch-opt"].outcome == Outcome.FAILED
        assert current.branches["branch-opt"].state == WorkState.FINISHED
        assert current.result is not None and current.result["words"] == 3
        await store.close()
    finally:
        await service.close()


async def test_unknown_optional_branch_blocks_root_success(tmp_path, platform_feedback) -> None:
    """An optional branch whose tool result is unknown is UNKNOWN/RECONCILING, not
    FAILED, and the root task cannot succeed while that work is unreconciled."""
    executor = FakeExecutor(delay=0.02)
    gateway = FakeGateway(platform_feedback)
    store, service = await started(tmp_path, platform_feedback, gateway, executor)
    try:
        spec = TaskCreate(
            documents={"notes.txt": "one two three"},
            branches=[
                BranchSpec(agent_id="branch-main", instruction="Main."),
                BranchSpec(agent_id="branch-opt", instruction="Optional.", required=False),
            ],
        )
        task = await service.create(spec)
        executor.fail_when = owned_by(service, branch_agent_id(task.task_id, "branch-opt"))
        await runner_finishes(service, task.task_id)
        current = service.get(task.task_id)
        assert current.outcome == Outcome.UNKNOWN
        assert current.state == WorkState.RECONCILING
        assert "BRANCH_UNRECONCILED" in (current.error or "")
        assert current.result is None and not current.quiescent
        optional = current.branches["branch-opt"]
        assert optional.outcome == Outcome.UNKNOWN
        assert optional.state == WorkState.RECONCILING
        assert not optional.quiescent
        assert current.branches["branch-main"].outcome == Outcome.SUCCEEDED
        assert current.branches["branch-main"].quiescent
        # The parent never started its summary while the branch was unreconciled.
        assert not any(r.agent_id == current.agent_id for r in gateway.submissions)
        await store.close()
    finally:
        await service.close()


async def test_resume_confirms_unknown_branch_tool_without_replay(
    tmp_path, platform_feedback
) -> None:
    """Restart chain: a transport failure leaves branch-a's tool attempt UNKNOWN and
    the root RECONCILING; resume confirms the original execution identity (no
    re-execution, no double settlement), then runs the dependent branch-b and the
    parent summary."""
    executor = FakeExecutor(delay=0.02)
    gateway = FakeGateway(platform_feedback)
    store, service = await started(tmp_path, platform_feedback, gateway, executor)
    try:
        spec = TaskCreate(
            documents={"notes.txt": "one two three"},
            branches=[
                BranchSpec(agent_id="branch-a", instruction="First."),
                BranchSpec(agent_id="branch-b", instruction="Second.", depends_on=["branch-a"]),
            ],
        )
        task = await service.create(spec)
        owner_a = branch_agent_id(task.task_id, "branch-a")
        executor.fail_when = owned_by(service, owner_a)
        await runner_finishes(service, task.task_id)
        current = service.get(task.task_id)
        assert current.outcome == Outcome.UNKNOWN
        assert current.state == WorkState.RECONCILING
        assert current.branches["branch-a"].outcome == Outcome.UNKNOWN
        assert current.branches["branch-a"].state == WorkState.RECONCILING
        assert current.branches["branch-b"].outcome == Outcome.PENDING
        tool_attempts = [a for a in service.attempts.values() if a.kind == "tool"]
        assert len(tool_attempts) == 1
        original = tool_attempts[0]
        assert original.agent_id == owner_a
        assert original.outcome == Outcome.UNKNOWN and not original.settled
        # Heal the executor and resume: the original attempt is confirmed.
        executor.healed = True
        resumed = await service.resume(task.task_id)
        assert resumed.task_id == task.task_id
        await runner_finishes(service, task.task_id)
        current = service.get(task.task_id)
        assert current.outcome == Outcome.SUCCEEDED, current.error
        assert current.result is not None and current.result["words"] == 3
        for name in ("branch-a", "branch-b"):
            assert current.branches[name].outcome == Outcome.SUCCEEDED
            assert current.branches[name].quiescent
        assert current.branches["branch-b"].cancel_generation == current.cancel_generation
        # branch-a's tool ran once on generation 0; then branch-b and the parent.
        assert len(executor.runs) == 3
        assert [r.attempt_id for r in executor.runs].count(original.attempt_id) == 1
        assert not [
            a
            for a in service.attempts.values()
            if a.kind == "tool" and a.agent_id == owner_a and a.attempt_id != original.attempt_id
        ]
        confirmed = service.attempts[original.attempt_id]
        assert confirmed.outcome == Outcome.SUCCEEDED and confirmed.settled
        assert {r.root_task_id for r in gateway.submissions} == {task.task_id}
        await store.close()
    finally:
        await service.close()


async def test_resume_reruns_failed_dependency_branch(tmp_path, platform_feedback) -> None:
    """A failed required branch is retried on resume, and the branch that depends on
    it then runs instead of failing on the previous generation's outcome."""
    executor = FakeExecutor()
    refuse = True

    async def flaky_model(messages, request):
        if refuse and is_branch(request, "branch-a"):
            raise DomainError("MODEL_REJECTED", "branch model refused")
        return await standard_model(messages, request)

    gateway = FakeGateway(platform_feedback, model=flaky_model)
    store, service = await started(tmp_path, platform_feedback, gateway, executor)
    try:
        spec = TaskCreate(
            documents={"notes.txt": "one two three"},
            branches=[
                BranchSpec(agent_id="branch-a", instruction="First."),
                BranchSpec(agent_id="branch-b", instruction="Second.", depends_on=["branch-a"]),
            ],
        )
        task = await service.create(spec)
        await runner_finishes(service, task.task_id)
        current = service.get(task.task_id)
        assert current.outcome == Outcome.FAILED
        assert "BRANCH_FAILED" in (current.error or "")
        assert current.branches["branch-a"].outcome == Outcome.FAILED
        refuse = False
        await service.resume(task.task_id)
        await runner_finishes(service, task.task_id)
        current = service.get(task.task_id)
        assert current.outcome == Outcome.SUCCEEDED, current.error
        for name in ("branch-a", "branch-b"):
            assert current.branches[name].outcome == Outcome.SUCCEEDED
            assert current.branches[name].error is None
        # The retry created a new model attempt rather than reusing the failed one.
        owner_a = branch_agent_id(task.task_id, "branch-a")
        first_round = [
            a
            for a in service.attempts.values()
            if a.agent_id == owner_a and a.step_id == f"{owner_a}-model-0"
        ]
        assert sorted(a.outcome for a in first_round) == [Outcome.FAILED, Outcome.SUCCEEDED]
        await store.close()
    finally:
        await service.close()


async def test_resume_summary_does_not_reinject_branch_results(tmp_path, platform_feedback) -> None:
    """The parent summary's tool result is unknown; resume continues the saved
    summary checkpoint without appending branch_results a second time."""
    executor = FakeExecutor()
    gateway = FakeGateway(platform_feedback)
    store, service = await started(tmp_path, platform_feedback, gateway, executor)
    try:
        task = await service.create(two_branch_spec())
        executor.fail_when = owned_by(service, task.agent_id)
        await runner_finishes(service, task.task_id)
        current = service.get(task.task_id)
        assert current.outcome == Outcome.UNKNOWN
        executor.healed = True
        await service.resume(task.task_id)
        await runner_finishes(service, task.task_id)
        current = service.get(task.task_id)
        assert current.outcome == Outcome.SUCCEEDED, current.error
        roles = [m["role"] for m in current.checkpoint.messages]
        assert roles == ["system", "user", "assistant", "tool", "assistant"]
        assert sum("branch_results" in str(m["content"]) for m in current.checkpoint.messages) == 1
        # Branches stayed SUCCEEDED and were not re-run on the resumed generation.
        assert len(executor.runs) == 3
        await store.close()
    finally:
        await service.close()


async def test_parent_summary_keeps_system_prompt_and_instruction(
    tmp_path, platform_feedback
) -> None:
    """The parent's first summary request keeps the system prompt and the original
    instruction, with branch results carried inside the same user request."""
    executor = FakeExecutor()
    gateway = FakeGateway(platform_feedback)
    store, service = await started(tmp_path, platform_feedback, gateway, executor)
    try:
        spec = two_branch_spec()
        spec.instruction = "Merge both parts into one summary."
        task = await service.create(spec)
        await runner_finishes(service, task.task_id)
        assert service.get(task.task_id).outcome == Outcome.SUCCEEDED
        first = next(r for r in gateway.submissions if r.agent_id == task.agent_id)
        assert [m["role"] for m in first.messages] == ["system", "user"]
        assert "file_stats" in first.messages[0]["content"]
        request = json.loads(first.messages[1]["content"])
        assert request["instruction"] == "Merge both parts into one summary."
        assert set(request["branch_results"]) == {"branch-a", "branch-b"}
        assert request["branch_results"]["branch-a"]["words"] == 3
        await store.close()
    finally:
        await service.close()


def test_branch_dependency_cycles_are_rejected() -> None:
    def spec(edges: dict[str, list[str]]) -> TaskCreate:
        return TaskCreate(
            branches=[
                BranchSpec(agent_id=name, instruction="x", depends_on=deps)
                for name, deps in edges.items()
            ]
        )

    with pytest.raises(ValidationError, match="cycle"):
        spec({"a": ["b"], "b": ["a"]})
    with pytest.raises(ValidationError, match="cycle"):
        spec({"a": ["c"], "b": ["a"], "c": ["b"], "d": []})
    # A diamond is acyclic.
    assert len(spec({"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"]}).branches) == 4


def test_chat_mode_rejects_branches() -> None:
    with pytest.raises(ValidationError, match="chat mode"):
        TaskCreate(
            mode="chat",
            message="hello",
            branches=[BranchSpec(agent_id="a", instruction="x")],
        )


async def test_unstartable_branches_fail_with_deadlock_code(tmp_path, platform_feedback) -> None:
    """Defence in depth: a cycle that bypassed validation fails the root with a
    stable error code instead of spinning the event loop."""
    executor = FakeExecutor()
    gateway = FakeGateway(platform_feedback)
    store, service = await started(tmp_path, platform_feedback, gateway, executor)
    try:
        task = await service.create(two_branch_spec())
        # The runner has not started yet; forge a cycle past the contract check.
        branches = service.tasks[task.task_id].specification.branches
        branches[0].depends_on = ["branch-b"]
        branches[1].depends_on = ["branch-a"]
        await runner_finishes(service, task.task_id, 5)
        current = service.get(task.task_id)
        assert current.outcome == Outcome.FAILED
        assert "BRANCH_DEADLOCK" in (current.error or "")
        assert not executor.runs and not gateway.submissions
        await store.close()
    finally:
        await service.close()


async def test_same_branch_names_in_two_tasks_keep_separate_records(
    tmp_path, platform_feedback
) -> None:
    """Branch labels are task-local: two tasks naming branch-a/branch-b keep their
    own agent and step records, so each task graph stays complete."""
    executor = FakeExecutor()
    gateway = FakeGateway(platform_feedback)
    store, service = await started(tmp_path, platform_feedback, gateway, executor)
    try:
        first = await service.create(two_branch_spec())
        second = await service.create(two_branch_spec())
        await runner_finishes(service, first.task_id)
        await runner_finishes(service, second.task_id)
        agents = await store.records("agent", first.tenant_id)
        steps = await store.records("step", first.tenant_id)
        for task in (first, second):
            assert service.get(task.task_id).outcome == Outcome.SUCCEEDED
            own = [row for row in agents if row["root_task_id"] == task.task_id]
            branch_rows = {row["label"]: row for row in own if row["parent_agent_id"]}
            assert set(branch_rows) == {"branch-a", "branch-b"}
            for label, row in branch_rows.items():
                assert row["agent_id"] == branch_agent_id(task.task_id, label)
                assert row["parent_agent_id"] == task.agent_id
                owned_steps = [s for s in steps if s["agent_id"] == row["agent_id"]]
                assert owned_steps
                assert all(s["root_task_id"] == task.task_id for s in owned_steps)
        await store.close()
    finally:
        await service.close()


async def test_cancel_parent_cascades_to_branches(tmp_path, platform_feedback) -> None:
    """Parent cancellation cascades: every in-flight branch is cancelled too."""
    executor = FakeExecutor(delay=5.0)
    gateway = FakeGateway(platform_feedback)
    store, service = await started(tmp_path, platform_feedback, gateway, executor)
    try:
        task = await service.create(two_branch_spec())
        deadline = time.monotonic() + 5
        while len(executor.runs) < 2 and time.monotonic() < deadline:  # noqa: ASYNC110
            await asyncio.sleep(0.005)
        assert len(executor.runs) >= 2
        cancelled = await service.cancel(task.task_id)
        assert cancelled.outcome == Outcome.CANCELLED
        for agent_id in ("branch-a", "branch-b"):
            assert cancelled.branches[agent_id].outcome == Outcome.CANCELLED
        await store.close()
    finally:
        await service.close()


async def test_branches_share_root_budget_and_tool_slots(tmp_path, platform_feedback) -> None:
    """Acceptance 2: more branches never raise the root budget or the local
    tool-slot ceiling (tool_slots=1 serializes all branch tools)."""
    executor = FakeExecutor(delay=0.1)
    gateway = FakeGateway(platform_feedback)
    store, service = await started(
        tmp_path,
        platform_feedback,
        gateway,
        executor,
        config=ClientConfig(max_inflight=4, tool_slots=1),
    )
    try:
        task = await service.create(two_branch_spec())
        await runner_finishes(service, task.task_id)
        current = service.get(task.task_id)
        assert current.outcome == Outcome.SUCCEEDED
        assert executor.peak_active <= 1
        assert current.consumed_units <= current.specification.budget_units
        assert {r.root_task_id for r in gateway.submissions} == {task.task_id}
        await store.close()
    finally:
        await service.close()
