import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from taskloom.client.dispatcher import Dispatcher, QuarantinedExecution
from taskloom.client.model_scheduler import ModelScheduler
from taskloom.client.ports import ExecutorPort, GatewayPort
from taskloom.contracts.models import (
    AttemptState,
    Checkpoint,
    ClientConfig,
    ConversationState,
    ConversationUpdate,
    GatewayRecord,
    JsonObject,
    ModelReply,
    ModelRequest,
    PlatformFeedback,
    RootRegistration,
    TaskCreate,
    TaskState,
    ToolCall,
    ToolRun,
    ToolState,
    digest,
    identity,
    utc_now,
)
from taskloom.domain.model import Agent, DomainError, Outcome, Resources, Step, WorkState
from taskloom.domain.ports import Repository, StoredRecord
from taskloom.runtime.agent import FILE_STATS_TOOL, run_agent

# L3 调度器的算法参数。刻意不进 ClientConfig：它们是调度策略的一部分，
# 改它们只应改这一处，装配层不需要知道。
_MODEL_QUANTUM = 100
_URGENCY_WINDOW_SECONDS = 60.0
_AGING_WINDOW_SECONDS = 20.0
_URGENCY_BURST = 2


class ClientService:
    def __init__(
        self,
        store: Repository,
        gateway: GatewayPort,
        executor: ExecutorPort,
        profile_dir: Path,
        config: ClientConfig,
    ) -> None:
        self.store = store
        self.gateway = gateway
        self.executor = executor
        self.directory = profile_dir
        self.config = config
        self.tasks: dict[str, TaskState] = {}
        self.conversations: dict[str, ConversationState] = {}
        self.attempts: dict[str, AttemptState] = {}
        self.runners: dict[str, asyncio.Task[None]] = {}
        self.lock = asyncio.Lock()
        self.models = ModelScheduler(
            max_inflight=config.max_inflight,
            coordinated=config.coordinated,
            quantum=_MODEL_QUANTUM,
            urgency_window=_URGENCY_WINDOW_SECONDS,
            aging_window=_AGING_WINDOW_SECONDS,
            urgency_burst=_URGENCY_BURST,
            feedback=self.gateway.feedback,
        )
        self.tools = Dispatcher(
            Resources(
                slots=config.tool_slots,
                memory_bytes=config.memory_budget_mib * 1048576,
                artifact_bytes=config.artifact_budget_mib * 1048576,
            )
        )
        self.reconciler: asyncio.Task[None] | None = None
        self.ready = False
        self.closing = False
        self.platform_feedback: PlatformFeedback | None = None
        self.platform_error: str | None = None

    async def start(self) -> None:
        binding = await self.store.get("profile", "platform-account")
        if binding is not None:
            self.config.tenant_id = binding["account_id"]
        for data in await self.store.records("conversation", self.config.tenant_id):
            conversation = ConversationState.model_validate(data)
            self.conversations[conversation.conversation_id] = conversation
        for data in await self.store.records("task", self.config.tenant_id):
            task = TaskState.model_validate(data)
            if task.conversation_id is None:
                task.conversation_id = task.task_id
                conversation = ConversationState(
                    conversation_id=task.task_id,
                    tenant_id=task.tenant_id,
                    title=task.specification.title,
                    created_at=task.created_at,
                    updated_at=task.created_at,
                )
                await self.store.apply(
                    [
                        self._conversation_record(conversation),
                        StoredRecord(
                            "task", task.task_id, task.tenant_id, task.model_dump(mode="json")
                        ),
                    ]
                )
                self.conversations[conversation.conversation_id] = conversation
            self.tasks[task.task_id] = task
        for data in await self.store.records("attempt", self.config.tenant_id):
            attempt = AttemptState.model_validate(data)
            self.attempts[attempt.attempt_id] = attempt
        await self.models.start()
        await self.tools.start()
        try:
            await self.refresh_platform()
        except Exception as error:
            self.platform_error = getattr(error, "code", "PLATFORM_UNAVAILABLE")
        await self.reconcile()
        for attempt in self.attempts.values():
            if attempt.kind == "tool" and not attempt.quiescent:
                if not self.tools.pool.reserve(attempt.attempt_id, self._tool_demand()):
                    raise RuntimeError("Unresolved tool occupancy exceeds the configured capacity")
                self.tools.quarantined.add(attempt.attempt_id)
        for task in list(self.tasks.values()):
            if task.outcome == Outcome.PENDING and not task.cancel_requested:
                task.state = WorkState.RECONCILING
                await self._save_task(task)
                self.runners[task.task_id] = asyncio.create_task(self._run(task.task_id))
        self.reconciler = asyncio.create_task(self._reconcile_loop())
        self.ready = True

    def _tool_demand(self) -> Resources:
        return Resources(slots=1, memory_bytes=134217728, artifact_bytes=3145728)

    def get(self, task_id: str) -> TaskState:
        if task_id not in self.tasks:
            raise DomainError("NOT_FOUND", "Task not found")
        return self.tasks[task_id].model_copy(deep=True)

    async def _save_task(self, task: TaskState, related: list[StoredRecord] | None = None) -> None:
        await self.store.apply(
            [
                StoredRecord("task", task.task_id, task.tenant_id, task.model_dump(mode="json")),
                *(related or []),
            ],
            {
                "schema_version": 1,
                "event_id": identity(),
                "tenant_id": task.tenant_id,
                "root_task_id": task.task_id,
                "agent_id": task.agent_id,
                "generation": task.cancel_generation,
                "type": "task." + task.state.lower(),
                "outcome": task.outcome,
                "occurred_at": utc_now(),
            },
        )
        self.tasks[task.task_id] = task.model_copy(deep=True)

    async def _save_attempt(self, attempt: AttemptState) -> None:
        async with self.lock:
            task = self.get(attempt.root_task_id)
            all_attempts = dict(self.attempts)
            all_attempts[attempt.attempt_id] = attempt
            related = [a for a in all_attempts.values() if a.root_task_id == task.task_id]
            task.consumed_units = sum(a.consumed_units for a in related)
            task.reserved_units = sum(a.reserved_units for a in related)
            previous = self.attempts.get(attempt.attempt_id)
            if (
                attempt.reserved_units > (previous.reserved_units if previous else 0)
                and task.consumed_units + task.reserved_units > task.specification.budget_units
            ):
                raise DomainError("PRIVATE_BUDGET", "Root task budget exhausted")
            task.quiescent = all(a.quiescent for a in related)
            if task.cancel_requested and task.quiescent:
                task.state = WorkState.FINISHED
            await self.store.apply(
                [
                    StoredRecord(
                        "attempt",
                        attempt.attempt_id,
                        attempt.tenant_id,
                        attempt.model_dump(mode="json"),
                    ),
                    StoredRecord(
                        "task", task.task_id, task.tenant_id, task.model_dump(mode="json")
                    ),
                ],
                {
                    "schema_version": 1,
                    "event_id": identity(),
                    "tenant_id": task.tenant_id,
                    "root_task_id": task.task_id,
                    "step_id": attempt.step_id,
                    "attempt_id": attempt.attempt_id,
                    "generation": attempt.generation,
                    "type": attempt.kind + "." + attempt.state.lower(),
                    "outcome": attempt.outcome,
                    "occurred_at": utc_now(),
                },
            )
            self.attempts[attempt.attempt_id] = attempt.model_copy(deep=True)
            self.tasks[task.task_id] = task.model_copy(deep=True)

    def artifact_size(self) -> int:
        directory = self.directory / "tasks"
        return sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())

    def verify_inputs(self, task: TaskState) -> None:
        inputs = self.directory / "tasks" / task.task_id / "inputs"
        paths = list(inputs.iterdir())
        if any(path.is_symlink() or not path.is_file() for path in paths):
            raise DomainError("INPUT_VERSION_MISMATCH", "Unexpected staged input object")
        actual = {path.name: path.read_bytes().decode("utf-8") for path in paths}
        if digest(actual) != task.input_version:
            raise DomainError("INPUT_VERSION_MISMATCH", "Staged file contents changed")

    @staticmethod
    def _conversation_record(conversation: ConversationState) -> StoredRecord:
        return StoredRecord(
            "conversation",
            conversation.conversation_id,
            conversation.tenant_id,
            conversation.model_dump(mode="json"),
        )

    def conversation(self, conversation_id: str) -> ConversationState:
        item = self.conversations.get(conversation_id)
        if item is None or item.tenant_id != self.config.tenant_id:
            raise DomainError("NOT_FOUND", "Conversation not found")
        return item.model_copy(deep=True)

    async def create_conversation(self, title: str) -> ConversationState:
        await self.refresh_platform()
        async with self.lock:
            if len(self.conversations) >= 256:
                raise DomainError("QUEUE_FULL", "Personal conversation capacity exhausted", True)
            conversation = ConversationState(
                conversation_id=identity(), tenant_id=self.config.tenant_id, title=title
            )
            await self.store.apply([self._conversation_record(conversation)])
            self.conversations[conversation.conversation_id] = conversation
            return conversation.model_copy(deep=True)

    async def update_conversation(
        self, conversation_id: str, update: ConversationUpdate
    ) -> ConversationState:
        async with self.lock:
            conversation = self.conversation(conversation_id)
            if update.title is not None:
                conversation.title = update.title
            if update.archived is not None:
                conversation.archived = update.archived
            conversation.updated_at = utc_now()
            await self.store.apply([self._conversation_record(conversation)])
            self.conversations[conversation_id] = conversation
            return conversation.model_copy(deep=True)

    def conversation_summaries(self) -> list[JsonObject]:
        result: list[JsonObject] = []
        for conversation in self.conversations.values():
            tasks = [
                t for t in self.tasks.values() if t.conversation_id == conversation.conversation_id
            ]
            result.append(
                {
                    **conversation.model_dump(mode="json"),
                    "task_count": len(tasks),
                    "active_count": sum(t.outcome == Outcome.PENDING for t in tasks),
                    "consumed_units": sum(t.consumed_units for t in tasks),
                }
            )
        return sorted(result, key=lambda row: row["updated_at"], reverse=True)

    async def create(
        self, specification: TaskCreate, conversation_id: str | None = None
    ) -> TaskState:
        await self.refresh_platform()
        async with self.lock:
            if (
                sum(t.outcome == Outcome.PENDING for t in self.tasks.values()) >= 32
                or len(self.tasks) >= 256
            ):
                raise DomainError("QUEUE_FULL", "Personal task capacity exhausted", True)
            size = self.artifact_size()
            demand = sum(len(content.encode()) for content in specification.documents.values())
            reserved = sum(r.artifact_bytes for r in self.tools.pool.allocations.values())
            if size + demand + reserved > self.config.artifact_budget_mib * 1048576:
                raise DomainError("ARTIFACT_BUDGET", "Personal artifact capacity exhausted")
            if conversation_id is not None:
                conversation = self.conversation(conversation_id)
                if conversation.archived:
                    raise DomainError(
                        "CONVERSATION_ARCHIVED", "Restore the conversation before sending"
                    )
            else:
                if len(self.conversations) >= 256:
                    raise DomainError(
                        "QUEUE_FULL", "Personal conversation capacity exhausted", True
                    )
                conversation = ConversationState(
                    conversation_id=identity(),
                    tenant_id=self.config.tenant_id,
                    title=specification.title,
                )
            conversation.updated_at = utc_now()
            task = TaskState(
                task_id=identity(),
                conversation_id=conversation.conversation_id,
                agent_id=identity(),
                tenant_id=self.config.tenant_id,
                specification=specification,
                input_version=digest(specification.documents),
                deadline_at=(
                    datetime.now(UTC) + timedelta(seconds=specification.deadline_seconds)
                ).isoformat(),
            )
            workspace = self.directory / "tasks" / task.task_id
            (workspace / "inputs").mkdir(parents=True)
            for name, content in specification.documents.items():
                (workspace / "inputs" / name).write_text(content, encoding="utf-8", newline="")
            (workspace / "input.json").write_text(
                json.dumps(
                    {
                        "input_version": task.input_version,
                        "tenant_id": task.tenant_id,
                    }
                )
            )
            agent = Agent(agent_id=task.agent_id, root_task_id=task.task_id)
            await self._save_task(
                task,
                [
                    StoredRecord("agent", agent.agent_id, task.tenant_id, asdict(agent)),
                    self._conversation_record(conversation),
                ],
            )
            self.conversations[conversation.conversation_id] = conversation
            self.runners[task.task_id] = asyncio.create_task(self._run(task.task_id))
            return task.model_copy(deep=True)

    async def _attempt(
        self, task_id: str, step: str, kind: Literal["model", "tool"]
    ) -> AttemptState:
        async with self.lock:
            task = self.get(task_id)
            old_id = task.step_attempts.get(step)
            if old_id:
                old = self.attempts[old_id]
                if old.outcome in (Outcome.SUCCEEDED, Outcome.UNKNOWN) or (
                    old.outcome == Outcome.PENDING
                    and (old.generation == task.cancel_generation or not old.quiescent)
                ):
                    return old.model_copy(deep=True)
            if task.cancel_requested:
                raise asyncio.CancelledError
            attempt = AttemptState(
                attempt_id=identity(),
                root_task_id=task_id,
                agent_id=task.agent_id,
                step_id=step,
                tenant_id=task.tenant_id,
                kind=kind,
                generation=task.cancel_generation,
            )
            task.step_attempts[step] = attempt.attempt_id
            predecessors = (
                tuple(f"{task_id}-tool-{call_id}" for call_id in task.checkpoint.completed_tools)
                if kind == "model"
                else (f"{task_id}-model-{task.checkpoint.model_round - 1}",)
            )
            for predecessor in predecessors:
                prior = task.step_attempts.get(predecessor)
                if prior is None or self.attempts[prior].outcome != Outcome.SUCCEEDED:
                    raise DomainError("DEPENDENCY_NOT_READY", "A revealed step is not complete")
            step_record = Step(
                step_id=step,
                root_task_id=task_id,
                agent_id=task.agent_id,
                kind=kind,
                input_version=task.input_version,
                predecessors=predecessors,
                current_attempt_id=attempt.attempt_id,
            )
            await self.store.apply(
                [
                    StoredRecord("task", task_id, task.tenant_id, task.model_dump(mode="json")),
                    StoredRecord(
                        "attempt",
                        attempt.attempt_id,
                        task.tenant_id,
                        attempt.model_dump(mode="json"),
                    ),
                    StoredRecord("step", step, task.tenant_id, asdict(step_record)),
                ]
            )
            self.tasks[task_id] = task.model_copy(deep=True)
            self.attempts[attempt.attempt_id] = attempt.model_copy(deep=True)
            return attempt

    async def _run(self, task_id: str) -> None:
        generation = self.tasks[task_id].cancel_generation
        try:
            async with self.lock:
                task = self.get(task_id)
                self.verify_inputs(task)
                task.state = WorkState.RUNNING
                await self._save_task(task)
            await self.gateway.register(
                RootRegistration(root_task_id=task_id, budget_units=task.specification.budget_units)
            )
            result = await run_agent(
                TaskHost(self, task_id, generation),
                task.specification,
                task.checkpoint,
                self.config.max_steps,
            )
            async with self.lock:
                current = self.get(task_id)
                if current.cancel_generation != generation or current.cancel_requested:
                    return
                self.verify_inputs(current)
                target = self.directory / "tasks" / task_id / "summary.json"
                temporary = target.with_suffix(".tmp")
                temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
                temporary.replace(target)
                current.result = result
                current.completed_at = utc_now()
                current.outcome = Outcome.SUCCEEDED
                current.state = WorkState.FINISHED
                await self._save_task(current)
        except asyncio.CancelledError:
            async with self.lock:
                current = self.get(task_id)
                current.outcome = Outcome.CANCELLED if current.cancel_requested else Outcome.UNKNOWN
                current.completed_at = utc_now()
                current.state = WorkState.FINISHED if current.quiescent else WorkState.RECONCILING
                await self._save_task(current)
        except Exception as error:
            async with self.lock:
                current = self.get(task_id)
                current.outcome = Outcome.FAILED if current.quiescent else Outcome.UNKNOWN
                current.completed_at = utc_now()
                current.state = WorkState.FINISHED if current.quiescent else WorkState.RECONCILING
                current.error = getattr(error, "code", type(error).__name__) + ": " + str(error)
                await self._save_task(current)
        finally:
            self.runners.pop(task_id, None)

    async def cancel(self, task_id: str) -> TaskState:
        async with self.lock:
            task = self.get(task_id)
            if task.outcome == Outcome.SUCCEEDED or (task.cancel_requested and task.quiescent):
                return task
            task.cancel_requested = True
            task.cancel_generation += 1
            task.outcome = Outcome.CANCELLED
            task.completed_at = utc_now()
            task.state = WorkState.FINISHED if task.quiescent else WorkState.RECONCILING
            await self._save_task(task)
        runner = self.runners.get(task_id)
        if runner:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        for original in list(self.attempts.values()):
            if (
                original.root_task_id == task_id
                and original.outcome == Outcome.PENDING
                and original.quiescent
                and original.reserved_units == 0
            ):
                cancelled = original.model_copy(deep=True)
                cancelled.outcome = Outcome.CANCELLED
                cancelled.state = WorkState.FINISHED
                cancelled.settled = True
                await self._save_attempt(cancelled)
        await self.reconcile()
        return self.get(task_id)

    async def resume(self, task_id: str) -> TaskState:
        async with self.lock:
            task = self.get(task_id)
            if task.outcome == Outcome.SUCCEEDED:
                return task
            if task_id in self.runners:
                return task
            if digest(task.specification.documents) != task.input_version:
                raise DomainError("INPUT_VERSION_MISMATCH", "Task input changed")
            self.verify_inputs(task)
            task.cancel_requested = False
            task.cancel_generation += 1
            task.outcome = Outcome.PENDING
            task.state = WorkState.RECONCILING
            task.error = None
            task.completed_at = None
            task.deadline_at = (
                datetime.now(UTC) + timedelta(seconds=task.specification.deadline_seconds)
            ).isoformat()
            await self._save_task(task)
            self.runners[task_id] = asyncio.create_task(self._run(task_id))
            return task

    async def _settle_model(self, attempt: AttemptState, record: GatewayRecord) -> None:
        attempt.reserved_units = record.reserved_units
        attempt.consumed_units = record.consumed_units
        attempt.quiescent = record.quiescent
        attempt.outcome = record.outcome
        attempt.state = WorkState.FINISHED if record.quiescent else WorkState.RECONCILING
        attempt.result = record.reply.model_dump(mode="json") if record.reply else None
        attempt.settled = record.state == "FINISHED"
        attempt.error = record.error
        await self._save_attempt(attempt)

    async def model(self, task_id: str, messages: list[JsonObject], round_index: int) -> ModelReply:
        attempt = await self._attempt(task_id, f"{task_id}-model-{round_index}", "model")
        if attempt.result is not None and attempt.outcome == Outcome.SUCCEEDED:
            return ModelReply.model_validate(attempt.result)
        task = self.get(task_id)
        if attempt.request_payload is None:
            request = ModelRequest(
                request_id=attempt.attempt_id,
                root_task_id=task_id,
                agent_id=task.agent_id,
                step_id=attempt.step_id,
                attempt_id=attempt.attempt_id,
                cancel_generation=attempt.generation,
                messages=messages,
                tools=[FILE_STATS_TOOL],
                deadline_at=task.deadline_at,
            )
            attempt.request_payload = request.model_dump(mode="json")
        request = ModelRequest.model_validate(attempt.request_payload)
        attempt.state = WorkState.WAIT_QUOTA
        await self._save_attempt(attempt)

        async def execute(_request: ModelRequest) -> GatewayRecord:
            current_task = self.get(task_id)
            if current_task.cancel_requested:
                raise asyncio.CancelledError
            if (
                current_task.consumed_units
                + current_task.reserved_units
                + max(0, request.reserve_units - attempt.reserved_units)
                > task.specification.budget_units
            ):
                raise DomainError("PRIVATE_BUDGET", "Root task budget exhausted")
            attempt.state = WorkState.RUNNING
            attempt.quiescent = False
            attempt.reserved_units = request.reserve_units
            await self._save_attempt(attempt)
            admitted = False
            try:
                record = await self.gateway.submit(request)
                admitted = True
                while record.state not in ("FINISHED", "UNKNOWN"):
                    await asyncio.sleep(0.05)
                    record = await self.gateway.query(request.request_id)
                await self._settle_model(attempt, record)
                return record
            except asyncio.CancelledError:
                try:
                    record = await self.gateway.cancel(request.request_id)
                    await self._settle_model(attempt, record)
                except Exception:
                    attempt.outcome = Outcome.UNKNOWN
                    attempt.state = WorkState.RECONCILING
                    await self._save_attempt(attempt)
                raise
            except DomainError as error:
                if not admitted and error.code != "DUPLICATE_ID_CONFLICT":
                    attempt.quiescent = True
                    attempt.reserved_units = 0
                    attempt.settled = True
                    attempt.outcome = Outcome.FAILED
                    attempt.state = WorkState.FINISHED
                    attempt.error = error.code
                    await self._save_attempt(attempt)
                raise
            except Exception:
                if not attempt.settled:
                    attempt.outcome = Outcome.UNKNOWN
                    attempt.state = WorkState.RECONCILING
                    await self._save_attempt(attempt)
                raise

        record = await self.models.submit(request, execute)
        if record.reply is None:
            raise DomainError("MODEL_" + record.outcome, record.error or record.state)
        return record.reply

    async def tool(self, task_id: str, call: ToolCall) -> JsonObject:
        attempt = await self._attempt(task_id, f"{task_id}-tool-{call.call_id}", "tool")
        if attempt.outcome == Outcome.SUCCEEDED and attempt.result is not None:
            return attempt.result
        task = self.get(task_id)
        attempt.state = WorkState.WAIT_RESOURCE
        await self._save_attempt(attempt)

        async def execute() -> JsonObject:
            if self.get(task_id).cancel_requested:
                raise asyncio.CancelledError
            if (
                self.artifact_size() + self._tool_demand().artifact_bytes
                > self.config.artifact_budget_mib * 1048576
            ):
                raise DomainError("ARTIFACT_BUDGET", "Retained artifacts fill the local budget")
            request = ToolRun(
                attempt_id=attempt.attempt_id,
                root_task_id=task_id,
                tenant_id=task.tenant_id,
                generation=attempt.generation,
                input_version=task.input_version,
                work_iterations=task.specification.work_iterations,
                emit_bytes=task.specification.emit_bytes,
            )
            attempt.state = WorkState.RUNNING
            attempt.quiescent = False
            await self._save_attempt(attempt)
            try:
                state = await self.executor.run(request)
                attempt.execution_ref = {
                    "pid": state.pid,
                    "cgroup_path": state.cgroup_path,
                    "attempt_id": attempt.attempt_id,
                }
                await self._save_attempt(attempt)
                while not state.quiescent and state.outcome == Outcome.PENDING:
                    await asyncio.sleep(0.05)
                    found = await self.executor.query(attempt.attempt_id)
                    if found is None:
                        raise DomainError("TOOL_UNKNOWN", "Executor lost the attempt")
                    state = found
                await self._settle_tool(attempt, state)
                if not state.quiescent:
                    raise QuarantinedExecution("Tool resources remain unconfirmed")
                if state.outcome != Outcome.SUCCEEDED or state.result is None:
                    raise DomainError("TOOL_" + state.outcome, state.error or "Tool failed")
                return state.result
            except asyncio.CancelledError:
                try:
                    stopped = await self.executor.cancel(
                        attempt.attempt_id, self.get(task_id).cancel_generation
                    )
                    cancelled_state = await self.executor.query(attempt.attempt_id)
                    if cancelled_state:
                        await self._settle_tool(attempt, cancelled_state)
                    elif stopped:
                        attempt.quiescent = True
                        attempt.outcome = Outcome.CANCELLED
                        await self._save_attempt(attempt)
                    if not stopped:
                        raise QuarantinedExecution("Cancellation still awaiting physical stop")
                except asyncio.CancelledError:
                    raise
                except QuarantinedExecution:
                    raise
                except Exception as error:
                    attempt.outcome = Outcome.UNKNOWN
                    await self._save_attempt(attempt)
                    raise QuarantinedExecution(
                        "Executor unavailable during cancellation"
                    ) from error
                raise
            except QuarantinedExecution:
                raise
            except DomainError as error:
                if error.code in ("EXECUTOR_FULL", "INPUT_VERSION_MISMATCH", "ATTEMPT_CONFLICT"):
                    attempt.quiescent = True
                    attempt.outcome = Outcome.FAILED
                    attempt.error = error.code
                    attempt.state = WorkState.FINISHED
                    await self._save_attempt(attempt)
                elif not attempt.quiescent:
                    raise QuarantinedExecution("Executor state remains unconfirmed") from error
                raise
            except Exception as error:
                attempt.outcome = Outcome.UNKNOWN
                await self._save_attempt(attempt)
                raise QuarantinedExecution("Executor transport failed") from error

        return await self.tools.submit(attempt.attempt_id, task_id, self._tool_demand(), execute)

    async def _settle_tool(self, attempt: AttemptState, state: ToolState) -> None:
        attempt.quiescent = state.quiescent
        attempt.outcome = state.outcome
        attempt.result = state.result
        attempt.error = state.error
        attempt.state = WorkState.FINISHED if state.quiescent else WorkState.RECONCILING
        await self._save_attempt(attempt)

    async def reconcile(self) -> None:
        for original in list(self.attempts.values()):
            if original.quiescent or original.outcome not in (Outcome.PENDING, Outcome.UNKNOWN):
                continue
            attempt = original.model_copy(deep=True)
            try:
                if attempt.kind == "model" and attempt.reserved_units:
                    record = await self.gateway.query(attempt.attempt_id)
                    if record.state == "FINISHED":
                        await self._settle_model(attempt, record)
                elif attempt.kind == "tool":
                    state = await self.executor.query(attempt.attempt_id)
                    if state is not None and state.quiescent:
                        await self._settle_tool(attempt, state)
                        self.tools.pool.release(attempt.attempt_id)
                        self.tools.quarantined.discard(attempt.attempt_id)
                        self.tools.wake.set()
            except Exception:
                continue

    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(0.2)
            await self.reconcile()
            try:
                await self.refresh_platform()
            except Exception as error:
                self.platform_error = getattr(error, "code", "PLATFORM_UNAVAILABLE")
            await asyncio.sleep(self.config.feedback_interval_seconds)

    async def bind_platform(self, feedback: PlatformFeedback) -> None:
        async with self.lock:
            binding = await self.store.get("profile", "platform-account")
            expected = {"platform_id": feedback.platform_id, "account_id": feedback.account_id}
            if binding is not None and binding != expected:
                raise DomainError(
                    "PLATFORM_ACCOUNT_CONFLICT",
                    "Use another profile for a different platform account",
                )
            if binding is None:
                if self.tasks and feedback.account_id != self.config.tenant_id:
                    raise DomainError(
                        "PLATFORM_ACCOUNT_CONFLICT", "Existing tasks belong to another account"
                    )
                await self.store.apply(
                    [StoredRecord("profile", "platform-account", feedback.account_id, expected)]
                )
            self.config.tenant_id = feedback.account_id
            self.platform_feedback = feedback.model_copy(deep=True)
            self.platform_error = None

    async def refresh_platform(self) -> PlatformFeedback:
        feedback = await self.gateway.feedback()
        await self.bind_platform(feedback)
        return feedback

    async def wait_platform(self, request: ModelRequest) -> None:
        """轮询平台反馈直到允许提交，预算不足直接报错。

        模型侧的准入已经交给 :class:`ModelScheduler`，
        :meth:`model` 不再走这里。保留它是因为它的判据是
        ``ACCOUNT_BUDGET`` 语义的唯一权威表述：不可恢复的费用不足只看
        预算减已消费，预留占用和速率窗口都属于可恢复等待。新增准入逻辑
        时以它为准，不要在这里改出第二套判断。
        """
        while True:
            feedback = await self.refresh_platform()
            if (
                feedback.account_budget_units - feedback.account_consumed_units
                < request.reserve_units
            ):
                raise DomainError("ACCOUNT_BUDGET", "Platform account budget exhausted")
            if not self.config.coordinated or (
                feedback.can_submit
                and feedback.account_remaining_units >= request.reserve_units
                and feedback.account_window_tokens_remaining >= request.reserve_units
            ):
                return
            if datetime.now(UTC) >= datetime.fromisoformat(request.deadline_at):
                raise DomainError(
                    "DEADLINE_EXPIRED", "Task deadline expired while waiting for platform capacity"
                )
            await asyncio.sleep(
                max(0.1, min(feedback.retry_after_seconds, self.config.feedback_interval_seconds))
            )

    def platform_status(self) -> JsonObject:
        return {
            "platform_url": self.config.platform_url,
            "connected": self.platform_feedback is not None and self.platform_error is None,
            "coordinated": self.config.coordinated,
            "feedback": self.platform_feedback.model_dump(mode="json")
            if self.platform_feedback
            else None,
            "error": self.platform_error,
        }

    def resources(self) -> JsonObject:
        # 模型侧由 ModelScheduler 自己管在途名额，这里仍按 Resources 的
        # 对外形状汇报容量与可用量，前端与验收脚本不用改。
        model_capacity = asdict(Resources(slots=self.config.max_inflight))
        return {
            "capacity": asdict(self.tools.pool.capacity),
            "available": asdict(self.tools.pool.available),
            "queued_tools": len(self.tools.queue),
            "queued_models": self.models.pending,
            "quarantined": list(self.tools.quarantined),
            "artifact_bytes": self.artifact_size(),
            "artifact_capacity_bytes": self.config.artifact_budget_mib * 1048576,
            "model_capacity": model_capacity,
            "model_available": {
                **model_capacity,
                "slots": max(0, self.config.max_inflight - self.models.in_flight),
            },
            "allocations": [
                {
                    "attempt_id": key,
                    "root_task_id": self.attempts[key].root_task_id
                    if key in self.attempts
                    else None,
                    "kind": kind,
                    "resources": asdict(demand),
                }
                for kind, pool in (("tool", self.tools.pool),)
                for key, demand in pool.allocations.items()
            ]
            + [
                {
                    "attempt_id": request_id,
                    "root_task_id": root_task_id,
                    "kind": "model",
                    "resources": asdict(Resources(slots=1)),
                }
                for request_id, root_task_id in self.models.in_flight_requests
            ],
            "attempts": [a.model_dump(mode="json") for a in self.attempts.values()],
        }

    async def close(self) -> None:
        self.ready = False
        self.closing = True
        runners = list(self.runners.values())
        for runner in runners:
            runner.cancel()
        await asyncio.gather(*runners, return_exceptions=True)
        if self.reconciler:
            self.reconciler.cancel()
            await asyncio.gather(self.reconciler, return_exceptions=True)
        await self.models.close()
        await self.tools.close()
        await self.gateway.close()
        await self.executor.close()


class TaskHost:
    def __init__(self, service: ClientService, task_id: str, generation: int) -> None:
        self.service = service
        self.task_id = task_id
        self.generation = generation

    async def model(self, messages: list[JsonObject], round_index: int) -> ModelReply:
        return await self.service.model(self.task_id, messages, round_index)

    async def tool(self, call: ToolCall) -> JsonObject:
        return await self.service.tool(self.task_id, call)

    async def checkpoint(self, checkpoint: Checkpoint) -> None:
        async with self.service.lock:
            task = self.service.get(self.task_id)
            if task.cancel_generation != self.generation or task.cancel_requested:
                raise asyncio.CancelledError
            task.checkpoint = checkpoint.model_copy(deep=True)
            await self.service._save_task(task)
