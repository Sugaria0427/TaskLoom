"""Controlled parallel sub-agents, dependency confluence and cancel/resume recovery.

Issue IKR9I1 acceptance:
- two real branches run in parallel and one summary step conflates them,
  recording the relative order change versus sequential execution;
- branch count never raises the root task budget or resource ceilings;
- a key branch-failure / parent-cancel / restart chain is verified, keeping the
  original identity of unknown executions to avoid double execution and settlement.
"""

import asyncio
import time

import pytest

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
    def __init__(self, delay: float = 0.0, fail: bool = False):
        self._delay = delay
        self._fail = fail
        self.runs: list[ToolRun] = []
        self.started_at: list[float] = []
        self.finished_at: list[float] = []
        self.active = 0
        self.peak_active = 0
        self.cancelled: list[str] = []

    async def run(self, request: ToolRun) -> ToolState:
        self.runs.append(request)
        self.started_at.append(time.monotonic())
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            if self._fail:
                raise RuntimeError("tool boom")
            return ToolState(
                request=request,
                outcome=Outcome.SUCCEEDED,
                quiescent=True,
                result={"files": 1, "words": 3},
                pid=1,
                start_ticks="1",
                boot_id="1",
                cgroup_path="/sys/fs/cgroup",
            )
        finally:
            self.active -= 1
            self.finished_at.append(time.monotonic())

    async def query(self, attempt_id: str) -> ToolState | None:
        # Simulate executor crash recovery: once healed, the original run of a
        # submitted attempt is confirmed SUCCEEDED instead of being re-executed.
        if self._fail:
            return None
        for request in self.runs:
            if request.attempt_id == attempt_id:
                return ToolState(
                    request=request,
                    outcome=Outcome.SUCCEEDED,
                    quiescent=True,
                    result={"files": 1, "words": 3},
                    pid=1,
                    start_ticks="1",
                    boot_id="1",
                    cgroup_path="/sys/fs/cgroup",
                )
        return None

    async def cancel(self, attempt_id: str, generation: int) -> bool:
        self.cancelled.append(attempt_id)
        return True

    async def close(self) -> None:
        pass


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
        assert agent_ids == {current.agent_id, "branch-a", "branch-b"}
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


async def test_required_branch_failure_fails_parent(tmp_path, platform_feedback) -> None:
    """A failing required branch fails the whole root task. A clean DomainError
    refusal settles the branch attempt, so the root task is FAILED, not UNKNOWN."""
    executor = FakeExecutor(delay=0.05)

    async def failing_model(messages, request):
        if request.agent_id in ("branch-a", "branch-b"):
            raise DomainError("MODEL_REJECTED", "branch model refused")
        return await standard_model(messages, request)

    gateway = FakeGateway(platform_feedback, model=failing_model)
    store, service = await started(tmp_path, platform_feedback, gateway, executor)
    try:
        task = await service.create(two_branch_spec())
        await runner_finishes(service, task.task_id)
        current = service.get(task.task_id)
        assert current.outcome == Outcome.FAILED
        assert "BRANCH_FAILED" in (current.error or "")
        for agent_id in ("branch-a", "branch-b"):
            assert current.branches[agent_id].outcome == Outcome.FAILED
        await store.close()
    finally:
        await service.close()


async def test_optional_branch_failure_parent_still_succeeds(tmp_path, platform_feedback) -> None:
    """A failing optional branch does not fail the parent; the parent conflates
    the succeeded branches."""
    executor = FakeExecutor(delay=0.05)

    async def failing_model(messages, request):
        if request.agent_id == "branch-opt":
            raise RuntimeError("optional branch rejected")
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
        assert current.branches["branch-main"].outcome == Outcome.SUCCEEDED
        assert current.branches["branch-opt"].outcome == Outcome.FAILED
        assert current.result is not None and current.result["words"] == 3
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


async def test_resume_reruns_failed_branch_with_root_identity(tmp_path, platform_feedback) -> None:
    """Restart chain: an executor crash leaves the branch attempt UNKNOWN and the
    root task RECONCILING; resume confirms the original execution identity
    (no re-execution, no double settlement) and the root succeeds."""
    executor = FakeExecutor(delay=0.02, fail=True)
    gateway = FakeGateway(platform_feedback)
    store, service = await started(tmp_path, platform_feedback, gateway, executor)
    try:
        task = await service.create(two_branch_spec())
        await runner_finishes(service, task.task_id)
        current = service.get(task.task_id)
        assert current.outcome == Outcome.UNKNOWN
        assert current.state == WorkState.RECONCILING
        for agent_id in ("branch-a", "branch-b"):
            assert current.branches[agent_id].outcome == Outcome.FAILED
        # The original tool attempt stayed UNKNOWN and unsettled: the identity is
        # preserved so resume can confirm it instead of re-executing.
        tool_attempts = [a for a in service.attempts.values() if a.kind == "tool"]
        assert tool_attempts and all(
            a.outcome == Outcome.UNKNOWN and not a.settled for a in tool_attempts
        )
        original_tool_ids = {a.attempt_id for a in tool_attempts}
        first_submissions = len(gateway.submissions)
        # Heal the executor and resume: the original attempts are confirmed and
        # the branches finish without re-running their tools.
        executor._fail = False
        resumed = await service.resume(task.task_id)
        assert resumed.task_id == task.task_id
        await runner_finishes(service, task.task_id)
        current = service.get(task.task_id)
        assert current.outcome == Outcome.SUCCEEDED
        assert current.result is not None and current.result["words"] == 3
        for agent_id in ("branch-a", "branch-b"):
            assert current.branches[agent_id].outcome == Outcome.SUCCEEDED
        # Branch tools never re-executed (two generation-0 runs); the third run is
        # the parent summary step's own file_stats on the resumed generation.
        assert len(executor.runs) == 3
        assert sum(1 for r in executor.runs if r.generation == 0) == 2
        tool_attempts_after = [a for a in service.attempts.values() if a.kind == "tool"]
        assert original_tool_ids <= {a.attempt_id for a in tool_attempts_after}
        # No branch tool attempt was created on the resumed generation: the
        # original unknown identity was confirmed, not re-executed.
        assert not [
            a
            for a in tool_attempts_after
            if a.agent_id in ("branch-a", "branch-b") and a.generation != 0
        ]
        assert len(gateway.submissions) > first_submissions
        assert {r.root_task_id for r in gateway.submissions} == {task.task_id}
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
