import asyncio
import base64
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from taskloom.client.checkpoint_upgrade import upgrade_checkpoint
from taskloom.client.dispatcher import Dispatcher, QuarantinedExecution
from taskloom.client.feedback_decision import request_decision
from taskloom.client.feedback_source import FeedbackSource
from taskloom.client.model_scheduler import ModelScheduler
from taskloom.client.ports import ExecutorPort, GatewayPort
from taskloom.contracts.models import (
    AttemptState,
    BranchState,
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
    ToolClaim,
    ToolPhase,
    ToolRun,
    ToolState,
    branch_agent_id,
    digest,
    identity,
    tool_step_id,
    utc_now,
)
from taskloom.domain.model import Agent, DomainError, Outcome, Resources, Step, WorkState
from taskloom.domain.ports import Repository, StoredRecord
from taskloom.execution.access import local_path, readable
from taskloom.execution.catalog import tool_profile
from taskloom.runtime.agent import run_agent
from taskloom.runtime.registry import ToolRegistry, agent_registry, default_registry

# L3 调度器的算法参数。刻意不进 ClientConfig：它们是调度策略的一部分，
# 改它们只应改这一处，装配层不需要知道。
_MODEL_QUANTUM = 100
_URGENCY_WINDOW_SECONDS = 60.0
_AGING_WINDOW_SECONDS = 20.0
_URGENCY_BURST = 2
# 阶段授权幂等；会话内响应丢失时有限次重发同一版本，超出后转入核对。
_PHASE_GRANT_SENDS = 3


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
        self.registry: ToolRegistry = default_registry(staged=config.staged_tools)
        self.chat_registry = agent_registry(staged=config.staged_tools)
        self.workspace_root = (
            Path(config.workspace_root).resolve()
            if config.workspace_root
            else (profile_dir / "workspace").resolve()
        )
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.approval_waiters: dict[str, asyncio.Future[bool]] = {}
        self.platform_feedback_source = FeedbackSource(
            self.gateway.feedback, config.feedback_interval_seconds
        )
        self.models = ModelScheduler(
            max_inflight=config.max_inflight,
            coordinated=config.coordinated,
            quantum=_MODEL_QUANTUM,
            urgency_window=_URGENCY_WINDOW_SECONDS,
            aging_window=_AGING_WINDOW_SECONDS,
            urgency_burst=_URGENCY_BURST,
            feedback=self.platform_feedback_source,
        )
        self.tools = Dispatcher(
            Resources(
                slots=config.tool_slots,
                memory_bytes=config.memory_budget_mib * 1048576,
                artifact_bytes=config.artifact_budget_mib * 1048576,
            ),
            check_safe=config.phase_banker,
            protect_large=config.large_tool_protection,
            protection_seconds=config.protection_seconds,
            bypass_limit=config.protection_bypasses,
        )
        self.reconciler: asyncio.Task[None] | None = None
        self.ready = False
        self.closing = False
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
        for original in list(self.tasks.values()):
            if original.outcome == Outcome.SUCCEEDED:
                continue
            try:
                await self._upgrade_checkpoint(original)
            except DomainError as error:
                held = original.model_copy(deep=True)
                held.state = WorkState.RECONCILING
                if not held.cancel_requested:
                    held.outcome = Outcome.UNKNOWN
                held.error = error.code + ": " + str(error)
                await self._save_task(held, await self._checkpoint_backup(original))
        await self.models.start()
        try:
            await self.refresh_platform()
        except Exception as error:
            self.platform_error = getattr(error, "code", "PLATFORM_UNAVAILABLE")
        await self.reconcile()
        for attempt in self.attempts.values():
            if attempt.kind == "tool" and not attempt.quiescent:
                if attempt.request_payload is None or "attempt_id" not in attempt.request_payload:
                    try:
                        receipt = await self.executor.query(attempt.attempt_id)
                    except Exception:
                        receipt = None
                    if receipt is not None and self._tool_receipt_matches(attempt, receipt):
                        attempt.request_payload = receipt.request.model_dump(mode="json")
                demand, request = self._prepare_tool(attempt)
                await self._save_attempt(attempt)
                self.tools.pool.restore(
                    attempt.attempt_id, demand, self._tool_maximum(demand, request)
                )
                self.tools.quarantined.add(attempt.attempt_id)
        await self.tools.start()
        for task in list(self.tasks.values()):
            if task.outcome == Outcome.PENDING and not task.cancel_requested:
                task.state = WorkState.RECONCILING
                await self._save_task(task)
                self.runners[task.task_id] = asyncio.create_task(self._run(task.task_id))
        self.reconciler = asyncio.create_task(self._reconcile_loop())
        self.ready = True

    async def _checkpoint_backup(self, task: TaskState) -> list[StoredRecord]:
        if await self.store.get("checkpoint_backup", task.task_id) is not None:
            return []
        return [
            StoredRecord(
                "checkpoint_backup",
                task.task_id,
                task.tenant_id,
                {
                    "task": task.model_dump(mode="json"),
                    "attempts": [
                        item.model_dump(mode="json")
                        for item in self.attempts.values()
                        if item.root_task_id == task.task_id
                    ],
                },
            )
        ]

    async def _upgrade_checkpoint(self, task: TaskState) -> TaskState:
        upgraded, changed = upgrade_checkpoint(task, self.attempts)
        if upgraded == task and not changed:
            return task
        records = await self._checkpoint_backup(task)
        records.extend(
            StoredRecord("attempt", item.attempt_id, item.tenant_id, item.model_dump(mode="json"))
            for item in changed
        )
        await self._save_task(upgraded, records)
        for item in changed:
            self.attempts[item.attempt_id] = item
        return upgraded

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

    async def _save_attempt(self, attempt: AttemptState, *, locked: bool = False) -> None:
        if locked:
            await self._persist_attempt(attempt)
        else:
            async with self.lock:
                await self._persist_attempt(attempt)

    async def _persist_attempt(self, attempt: AttemptState) -> None:
        """Persist under self.lock; a settled identity cannot regress to an older reply."""
        previous = self.attempts.get(attempt.attempt_id)
        if previous is not None and previous.settled:
            return
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
        for name, branch in task.branches.items():
            owner = branch_agent_id(task.task_id, name)
            owned = [a for a in related if a.agent_id == owner]
            branch.consumed_units = sum(a.consumed_units for a in owned)
            branch.reserved_units = sum(a.reserved_units for a in owned)
            branch.quiescent = all(a.quiescent for a in owned)
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
                StoredRecord("task", task.task_id, task.tenant_id, task.model_dump(mode="json")),
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
                "resource_phase": attempt.phase_version,
                "resource_allocation": attempt.phase_allocation.model_dump(mode="json")
                if attempt.phase_allocation
                else None,
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
            demand += sum(
                len(base64.b64decode(image.data_base64)) for image in specification.images
            )
            reserved = sum(r.artifact_bytes for r in self.tools.pool.allocations.values())
            if size + demand + reserved > self.config.artifact_budget_mib * 1048576:
                raise DomainError("ARTIFACT_BUDGET", "Personal artifact capacity exhausted")
            if conversation_id is not None:
                conversation = self.conversation(conversation_id)
                if conversation.archived:
                    raise DomainError(
                        "CONVERSATION_ARCHIVED", "Restore the conversation before sending"
                    )
                if specification.mode == "chat" and any(
                    t.conversation_id == conversation_id and t.outcome == Outcome.PENDING
                    for t in self.tasks.values()
                ):
                    raise DomainError(
                        "CONVERSATION_BUSY", "当前对话仍在执行，请等待完成或新建对话。"
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
            if specification.images:
                (workspace / "attachments").mkdir()
                for image in specification.images:
                    (workspace / "attachments" / image.name).write_bytes(
                        base64.b64decode(image.data_base64)
                    )
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
        self,
        task_id: str,
        step: str,
        kind: Literal["model", "tool"],
        request_payload: JsonObject | None = None,
        model_round: int | None = None,
        branch: str | None = None,
    ) -> AttemptState:
        async with self.lock:
            task = self.get(task_id)
            identity_agent = branch_agent_id(task_id, branch) if branch else task.agent_id
            prefix = identity_agent if branch else task_id
            old_id = task.step_attempts.get(step)
            if old_id:
                old = self.attempts[old_id]
                if old.kind != kind or (
                    request_payload is not None
                    and (
                        old.request_payload is None
                        or any(
                            old.request_payload.get(key) != value
                            for key, value in request_payload.items()
                        )
                    )
                ):
                    raise DomainError(
                        "ATTEMPT_CONFLICT", "A step identity was reused for a different request"
                    )
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
                agent_id=identity_agent,
                step_id=step,
                tenant_id=task.tenant_id,
                kind=kind,
                generation=task.cancel_generation,
                request_payload=request_payload,
            )
            task.step_attempts[step] = attempt.attempt_id
            checkpoint = task.branches[branch].checkpoint if branch else task.checkpoint
            tool_model_round = (
                model_round if model_round is not None else checkpoint.model_round - 1
            )
            predecessors = (
                tuple(task.tool_step_aliases.get(key, key) for key in checkpoint.completed_tools)
                if kind == "model"
                else (f"{prefix}-model-{tool_model_round}",)
            )
            for predecessor in predecessors:
                prior = task.step_attempts.get(predecessor)
                observation = self.attempts.get(prior) if prior else None
                observed_failure = (
                    task.specification.mode == "chat"
                    and kind == "model"
                    and observation is not None
                    and observation.kind == "tool"
                    and observation.quiescent
                    and observation.settled
                    and observation.result is not None
                    and (
                        observation.result.get("approved") is False or "error" in observation.result
                    )
                )
                if not observed_failure and (
                    observation is None or observation.outcome != Outcome.SUCCEEDED
                ):
                    raise DomainError("DEPENDENCY_NOT_READY", "A revealed step is not complete")
            step_record = Step(
                step_id=step,
                root_task_id=task_id,
                agent_id=identity_agent,
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
            task = self.get(task_id)
            if task.specification.branches:
                result = await self._run_with_branches(task_id, generation)
            else:
                result = await run_agent(
                    TaskHost(self, task_id, generation),
                    task_id,
                    task.specification,
                    task.checkpoint,
                    self.config.max_steps,
                    self.chat_registry if task.specification.mode == "chat" else self.registry,
                    history=self._chat_history(task),
                    workspace=str(self.workspace_root),
                    attachments={
                        **{
                            name: str(self.directory / "tasks" / task_id / "inputs" / name)
                            for name in task.specification.documents
                        },
                        **{
                            image.name: str(
                                self.directory / "tasks" / task_id / "attachments" / image.name
                            )
                            for image in task.specification.images
                        },
                    },
                )
            async with self.lock:
                current = self.get(task_id)
                if current.cancel_generation != generation or current.cancel_requested:
                    return
                self.verify_inputs(current)
                if current.specification.mode != "chat":
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
                # A branch with an unknown result keeps the root in reconciliation
                # even when its attempt has already stopped.
                settled = current.quiescent and getattr(error, "code", "") != "BRANCH_UNRECONCILED"
                current.outcome = Outcome.FAILED if settled else Outcome.UNKNOWN
                current.completed_at = utc_now()
                current.state = WorkState.FINISHED if settled else WorkState.RECONCILING
                current.error = getattr(error, "code", type(error).__name__) + ": " + str(error)
                await self._save_task(current)
        finally:
            self.runners.pop(task_id, None)
            self._retire_model_roots()

    def _retire_model_roots(self) -> None:
        for task in self.tasks.values():
            if task.quiescent and task.outcome in (
                Outcome.SUCCEEDED,
                Outcome.FAILED,
                Outcome.CANCELLED,
            ):
                self.models.forget_root(task.task_id)

    async def _run_with_branches(self, task_id: str, generation: int) -> JsonObject:
        """Run controlled parallel sub-agents, wait for confluence, then let the
        parent agent produce the final summary.

        While the parent waits it holds no model or tool request, so its
        execution permit is safely yielded to the branches. Every branch
        request keeps ``root_task_id=task_id``: no matter how many branches run,
        budget and L3/L4 fair share stay on the root task.

        Confluence is fixed once the parent summary has written its checkpoint;
        until then every new generation retries the branches that did not succeed.
        """
        async with self.lock:
            task = self.get(task_id)
            confluent = bool(task.checkpoint.messages)
            agent_records: list[StoredRecord] = []
            for spec in task.specification.branches:
                branch = task.branches.get(spec.agent_id)
                if branch is None:
                    task.branches[spec.agent_id] = BranchState(
                        agent_id=spec.agent_id,
                        root_task_id=task_id,
                        parent_agent_id=task.agent_id,
                        instruction=spec.instruction,
                        required=spec.required,
                        depends_on=list(spec.depends_on),
                    )
                    agent = Agent(
                        agent_id=branch_agent_id(task_id, spec.agent_id),
                        root_task_id=task_id,
                        parent_agent_id=task.agent_id,
                        label=spec.agent_id,
                    )
                    agent_records.append(
                        StoredRecord("agent", agent.agent_id, task.tenant_id, asdict(agent))
                    )
                elif branch.outcome != Outcome.SUCCEEDED and not confluent:
                    # Keep the checkpoint: an unknown attempt is then confirmed under
                    # its original identity instead of being replayed.
                    branch.state = WorkState.READY
                    branch.outcome = Outcome.PENDING
                    branch.result = None
                    branch.error = None
                    branch.completed_at = None
            task.state = WorkState.RUNNING if confluent else WorkState.WAIT_DEPENDENCY
            await self._save_task(task, agent_records)
        results: JsonObject | None = None
        if not confluent:
            results = await self._await_branches(task_id, generation)
            async with self.lock:
                task = self.get(task_id)
                if task.cancel_generation != generation or task.cancel_requested:
                    raise asyncio.CancelledError
                task.state = WorkState.RUNNING
                await self._save_task(task)
        task = self.get(task_id)
        return await run_agent(
            TaskHost(self, task_id, generation),
            task_id,
            task.specification,
            task.checkpoint,
            self.config.max_steps,
            self.registry,
            branch_results=results,
        )

    async def _await_branches(self, task_id: str, generation: int) -> JsonObject:
        """Start branches in dependency order until every one is terminal.

        A required branch that ends without success fails the root at once and
        the ``finally`` cancels its siblings. Returns the successful results.
        """
        pending = {
            name
            for name, branch in self.get(task_id).branches.items()
            if branch.outcome != Outcome.SUCCEEDED
        }
        runners: dict[str, asyncio.Task[None]] = {}
        try:
            while pending:
                progressed = False
                async with self.lock:
                    task = self.get(task_id)
                    if task.cancel_generation != generation or task.cancel_requested:
                        raise asyncio.CancelledError
                    for name in [name for name, runner in runners.items() if runner.done()]:
                        runners.pop(name)
                        pending.discard(name)
                        progressed = True
                        branch = task.branches[name]
                        if branch.required and branch.outcome != Outcome.SUCCEEDED:
                            raise self._branch_error(name, branch)
                    startable: list[str] = []
                    for name in sorted(pending - runners.keys()):
                        branch = task.branches[name]
                        blocked = next(
                            (
                                dependency
                                for dependency in branch.depends_on
                                if dependency not in pending
                                and task.branches[dependency].outcome != Outcome.SUCCEEDED
                            ),
                            None,
                        )
                        if blocked is None:
                            if all(
                                task.branches[dependency].outcome == Outcome.SUCCEEDED
                                for dependency in branch.depends_on
                            ):
                                startable.append(name)
                            continue
                        if branch.required:
                            raise DomainError(
                                "BRANCH_DEPENDENCY_FAILED",
                                f"Branch {name} waits on unsuccessful branch {blocked}",
                            )
                        # An optional branch whose dependency ended without success
                        # never starts, so it owns no attempt to reconcile.
                        branch.outcome = Outcome.FAILED
                        branch.state = WorkState.FINISHED
                        branch.error = f"BRANCH_DEPENDENCY_FAILED: {blocked} did not succeed"
                        branch.completed_at = utc_now()
                        pending.discard(name)
                        progressed = True
                        await self._save_task(task)
                for name in startable:
                    runners[name] = asyncio.create_task(self._run_branch(task_id, name, generation))
                    progressed = True
                if not runners:
                    if pending and not progressed:
                        raise DomainError(
                            "BRANCH_DEADLOCK",
                            "Branches can never start: " + ", ".join(sorted(pending)),
                        )
                    continue
                await asyncio.wait(runners.values(), return_when=asyncio.FIRST_COMPLETED)
        finally:
            for runner in runners.values():
                runner.cancel()
            if runners:
                await asyncio.gather(*runners.values(), return_exceptions=True)
        task = self.get(task_id)
        unreconciled = sorted(
            name for name, branch in task.branches.items() if branch.outcome == Outcome.UNKNOWN
        )
        if unreconciled:
            # Optional or not, unconfirmed work must be reconciled before any summary.
            raise DomainError(
                "BRANCH_UNRECONCILED",
                "Branches hold unreconciled work: " + ", ".join(unreconciled),
            )
        return {
            name: branch.result
            for name, branch in task.branches.items()
            if branch.outcome == Outcome.SUCCEEDED and branch.result is not None
        }

    @staticmethod
    def _branch_error(name: str, branch: BranchState) -> DomainError:
        if branch.outcome == Outcome.UNKNOWN:
            return DomainError("BRANCH_UNRECONCILED", f"Required branch {name} holds unknown work")
        return DomainError(
            "BRANCH_FAILED", f"Required branch {name} failed: {branch.error or branch.outcome}"
        )

    def _branch_unreconciled(self, task_id: str, name: str) -> bool:
        """Whether a branch still owns a dispatched attempt with an unknown result."""
        owner = branch_agent_id(task_id, name)
        return any(
            attempt.root_task_id == task_id
            and attempt.agent_id == owner
            and (
                not attempt.quiescent
                or (attempt.outcome == Outcome.UNKNOWN and not attempt.settled)
            )
            for attempt in self.attempts.values()
        )

    async def _run_branch(self, task_id: str, name: str, generation: int) -> None:
        """Execute one controlled sub-agent to its own bounded loop and record
        its durable outcome inside TaskState.branches."""
        try:
            async with self.lock:
                task = self.get(task_id)
                branch = task.branches[name]
                branch.state = WorkState.RUNNING
                branch.cancel_generation = generation
                await self._save_task(task)
            result = await run_agent(
                BranchHost(self, task_id, name, generation),
                task_id,
                task.specification,
                branch.checkpoint,
                self.config.max_steps,
                self.registry,
                agent_id=branch_agent_id(task_id, name),
                instruction=branch.instruction,
            )
            async with self.lock:
                task = self.get(task_id)
                branch = task.branches[name]
                if task.cancel_generation != generation or task.cancel_requested:
                    return
                branch.result = result
                branch.completed_at = utc_now()
                branch.outcome = Outcome.SUCCEEDED
                branch.state = WorkState.FINISHED
                await self._save_task(task)
        except asyncio.CancelledError:
            await self._finish_branch(task_id, name, None)
            raise
        except Exception as error:
            await self._finish_branch(task_id, name, error)

    async def _finish_branch(self, task_id: str, name: str, error: Exception | None) -> None:
        """Record a branch that stopped without a result.

        The outcome follows the branch's own attempts: any dispatched attempt whose
        result is still unknown makes the branch UNKNOWN/RECONCILING, never FAILED.
        """
        async with self.lock:
            task = self.get(task_id)
            branch = task.branches[name]
            unreconciled = self._branch_unreconciled(task_id, name)
            if error is None:
                cancelled = task.cancel_requested or not unreconciled
                branch.outcome = Outcome.CANCELLED if cancelled else Outcome.UNKNOWN
            else:
                branch.outcome = Outcome.UNKNOWN if unreconciled else Outcome.FAILED
                branch.error = getattr(error, "code", type(error).__name__) + ": " + str(error)
            branch.completed_at = utc_now()
            branch.state = WorkState.RECONCILING if unreconciled else WorkState.FINISHED
            await self._save_task(task)

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
            extra = []
            if task.pending_approval and task.pending_approval["status"] == "PENDING":
                task.pending_approval["status"] = "CANCELLED"
                extra = [
                    StoredRecord(
                        "approval",
                        task.pending_approval["approval_id"],
                        task.tenant_id,
                        task.pending_approval,
                    )
                ]
            await self._save_task(task, extra)
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

    async def update_priority(
        self, task_id: str, priority: Literal["low", "normal", "high"]
    ) -> TaskState:
        if priority not in {"low", "normal", "high"}:
            raise DomainError("INVALID_PRIORITY", "Priority must be low, normal, or high")
        async with self.lock:
            task = self.get(task_id)
            if task.outcome != Outcome.PENDING or task.cancel_requested:
                raise DomainError("TASK_NOT_PENDING", "Only pending tasks can change priority")
            task.specification.priority = priority
            await self._save_task(task)
            for attempt in self.attempts.values():
                if attempt.root_task_id == task_id and attempt.outcome == Outcome.PENDING:
                    self.models.set_priority(attempt.attempt_id, priority)
                    self.tools.set_priority(attempt.attempt_id, priority)
            return self.get(task_id)

    async def resume(self, task_id: str) -> TaskState:
        async with self.lock:
            task = self.get(task_id)
            if task.outcome == Outcome.SUCCEEDED:
                return task
            if task_id in self.runners:
                return task
            task = await self._upgrade_checkpoint(task)
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

    @staticmethod
    def _reserved_units(attempt: AttemptState) -> int:
        """派发时按原请求记下的预扣量。

        平台完成结算后会把 ``GatewayRecord.reserved_units`` 清零，所以不能
        拿它当原预扣量；原请求持久化在 ``AttemptState.request_payload`` 里，
        重启后也能复原。
        """
        if attempt.fair_charge_units is not None:
            return attempt.fair_charge_units
        if attempt.request_payload is not None:
            try:
                return ModelRequest.model_validate(attempt.request_payload).reserve_units
            except ValueError:
                pass
        return attempt.reserved_units

    async def _settle_model(self, attempt: AttemptState, record: GatewayRecord) -> GatewayRecord:
        async with self.lock:
            current = self.attempts.get(attempt.attempt_id)
            if current is not None and current.settled:
                # Both polling paths must consume the saved final result, even when an
                # older UNKNOWN response arrives after reconciliation has finished.
                return record.model_copy(
                    update={
                        "state": "FINISHED",
                        "outcome": current.outcome,
                        "reserved_units": current.reserved_units,
                        "consumed_units": current.consumed_units,
                        "quiescent": current.quiescent,
                        "error": current.error,
                        "reply": ModelReply.model_validate(current.result)
                        if current.result
                        else None,
                    }
                )
            reserved = self._reserved_units(attempt)
            attempt.reserved_units = record.reserved_units
            attempt.consumed_units = record.consumed_units
            attempt.quiescent = record.quiescent
            attempt.outcome = record.outcome
            attempt.state = WorkState.FINISHED if record.quiescent else WorkState.RECONCILING
            attempt.result = record.reply.model_dump(mode="json") if record.reply else None
            attempt.execution_ref = {
                "platform_created_at": record.created_at,
                "platform_dispatched_at": record.dispatched_at,
                "platform_completed_at": record.completed_at,
            }
            attempt.error = record.error
            attempt.settled = record.state == "FINISHED"
            await self._save_attempt(attempt, locked=True)
            # Commit the final marker before applying its in-memory fairness correction.
            if attempt.settled:
                self.models.settle(attempt.root_task_id, reserved, record.consumed_units)
            return record

    async def model(
        self,
        task_id: str,
        messages: list[JsonObject],
        round_index: int,
        branch: str | None = None,
    ) -> ModelReply:
        prefix = branch_agent_id(task_id, branch) if branch else task_id
        attempt = await self._attempt(
            task_id, f"{prefix}-model-{round_index}", "model", branch=branch
        )
        if attempt.result is not None and attempt.outcome == Outcome.SUCCEEDED:
            return ModelReply.model_validate(attempt.result)
        task = self.get(task_id)
        if attempt.request_payload is None:
            request = ModelRequest(
                request_id=attempt.attempt_id,
                root_task_id=task_id,
                agent_id=attempt.agent_id,
                step_id=attempt.step_id,
                attempt_id=attempt.attempt_id,
                cancel_generation=attempt.generation,
                messages=messages,
                max_tokens=2048 if task.specification.mode == "chat" else 512,
                tools=(
                    self.chat_registry if task.specification.mode == "chat" else self.registry
                ).schemas(),
                deadline_at=task.deadline_at,
                token_count_mode="utf8-bound"
                if self.platform_feedback_source.latest is not None
                and self.platform_feedback_source.latest.pool_kind == "fixed-http"
                else "fixture",
            )
            attempt.request_payload = request.model_dump(mode="json")
        request = ModelRequest.model_validate(attempt.request_payload)
        if attempt.fair_charge_units is None:
            feedback = self.platform_feedback_source.latest
            attempt.fair_charge_units = (
                feedback.admission_needs(request)[0]
                if feedback is not None
                else request.reserve_units
            )
        attempt.state = WorkState.WAIT_QUOTA
        await self._save_attempt(attempt)

        async def execute(_request: ModelRequest) -> GatewayRecord:
            current_task = self.get(task_id)
            if current_task.cancel_requested:
                raise asyncio.CancelledError
            worst = self._reserved_units(attempt)
            if (
                current_task.consumed_units
                + current_task.reserved_units
                + max(0, worst - attempt.reserved_units)
                > task.specification.budget_units
            ):
                raise DomainError("PRIVATE_BUDGET", "Root task budget exhausted")
            attempt.state = WorkState.RUNNING
            attempt.quiescent = False
            attempt.reserved_units = worst
            await self._save_attempt(attempt)
            admitted = False
            try:
                record = await self.gateway.submit(request)
                admitted = True
                while record.state not in ("FINISHED", "UNKNOWN"):
                    await asyncio.sleep(0.05)
                    record = await self.gateway.query(request.request_id)
                return await self._settle_model(attempt, record)
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
                    attempt.outcome = Outcome.FAILED
                    attempt.state = WorkState.FINISHED
                    attempt.error = error.code
                    # 请求没到过平台，实际消费为零，把派发时记的份额原样退回。
                    if not attempt.settled:
                        attempt.settled = True
                        self.models.settle(attempt.root_task_id, self._reserved_units(attempt), 0)
                    await self._save_attempt(attempt)
                raise
            except Exception:
                if not attempt.settled:
                    attempt.outcome = Outcome.UNKNOWN
                    attempt.state = WorkState.RECONCILING
                    await self._save_attempt(attempt)
                raise

        record = await self.models.submit(
            request,
            execute,
            cost_units=attempt.fair_charge_units,
            priority=self.get(task_id).specification.priority,
        )
        if record.reply is None:
            raise DomainError("MODEL_" + record.outcome, record.error or record.state)
        return record.reply

    def _prepare_tool(
        self, attempt: AttemptState, call: ToolCall | None = None
    ) -> tuple[Resources, ToolRun]:
        """Pin execution and admission to this Attempt, including after a restart.

        Pre-catalog checkpoints only launched file_stats. A saved executor receipt
        takes precedence over that legacy fallback during startup migration.
        """
        task = self.get(attempt.root_task_id)
        if attempt.request_payload is None or "attempt_id" not in attempt.request_payload:
            if call is None and attempt.request_payload is not None:
                call = ToolCall(
                    call_id="saved",
                    name=attempt.request_payload["tool_name"],
                    arguments=attempt.request_payload["arguments"],
                )
            if call is None:
                call = next(
                    (
                        c
                        for c in task.checkpoint.pending_tools
                        if attempt.step_id == f"{task.task_id}-tool-{c.call_id}"
                    ),
                    ToolCall(call_id="legacy", name="file_stats"),
                )
            profile = tool_profile(call.name)
            request = ToolRun(
                attempt_id=attempt.attempt_id,
                root_task_id=task.task_id,
                tenant_id=task.tenant_id,
                generation=attempt.generation,
                tool_name=profile.name,
                input_version=task.input_version,
                work_iterations=task.specification.work_iterations,
                emit_bytes=task.specification.emit_bytes,
                memory_bytes=profile.demand.memory_bytes,
                resource_protocol="phase-v1" if profile.maximum else "one-shot",
                maximum_memory_bytes=profile.maximum.memory_bytes if profile.maximum else None,
                cpu_quota=profile.cpu_quota,
                cpu_period=profile.cpu_period,
                arguments=call.arguments,
                workspace_root=str(self.workspace_root)
                if call.name in ("read_file", "run_command")
                else None,
                approved_external_path=(task.pending_approval or {}).get("path")
                if call.name == "read_file"
                else None,
                command_approved=call.name == "run_command",
                approved_command_cwd=(task.pending_approval or {}).get("cwd")
                if call.name == "run_command"
                else None,
                timeout_seconds=call.arguments["timeout_seconds"]
                if call.name == "run_command"
                else 60,
            )
            attempt.request_payload = request.model_dump(mode="json")
        request = ToolRun.model_validate(attempt.request_payload)
        if attempt.tool_claim is None:
            profile = tool_profile(request.tool_name)
            attempt.tool_claim = ToolClaim(
                slots=profile.demand.slots,
                memory_bytes=request.memory_bytes,
                artifact_bytes=profile.demand.artifact_bytes,
            )
        claim = attempt.tool_claim
        if claim.memory_bytes != request.memory_bytes:
            raise DomainError("TOOL_CLAIM_CONFLICT", "Saved claim differs from executor request")
        if attempt.phase_version:
            allocation = attempt.phase_allocation
            if (
                request.resource_protocol != "phase-v1"
                or allocation is None
                or allocation.memory_bytes != (128 + attempt.phase_version * 128) * 1048576
                or allocation.slots != claim.slots
                or allocation.artifact_bytes != claim.artifact_bytes
            ):
                raise DomainError("TOOL_CLAIM_CONFLICT", "Saved phase allocation differs")
            claim = allocation
        return Resources(claim.slots, claim.memory_bytes, claim.artifact_bytes), request

    @staticmethod
    def _tool_maximum(demand: Resources, request: ToolRun) -> Resources:
        return Resources(
            demand.slots, request.maximum_memory_bytes or demand.memory_bytes, demand.artifact_bytes
        )

    async def _advance_tool_phase(
        self, attempt: AttemptState, state: ToolState, generation: int
    ) -> None:
        phase = state.pending_phase
        if phase is None or state.quiescent:
            return
        if not self._tool_receipt_matches(attempt, state) or (
            state.request.resource_protocol != "phase-v1"
            or phase.attempt_id != attempt.attempt_id
            or phase.generation != attempt.generation
            or phase.memory_bytes != (128 + phase.version * 128) * 1048576
        ):
            raise QuarantinedExecution("Phase request differs from the saved tool identity")
        async with self.lock:
            current = self.attempts[attempt.attempt_id]
            task = self.get(attempt.root_task_id)
            if task.cancel_requested or task.cancel_generation != generation:
                raise asyncio.CancelledError
            if current.settled:
                return
            attempt.phase_version = current.phase_version
            attempt.phase_allocation = current.phase_allocation
            if phase.version > current.phase_version + 1:
                raise QuarantinedExecution("Phase request skipped a predecessor")
            if phase.version < current.phase_version:
                return
            demand, request = self._prepare_tool(attempt)
            target = Resources(demand.slots, phase.memory_bytes, demand.artifact_bytes)
            if not target.fits(self._tool_maximum(demand, request)):
                raise QuarantinedExecution("Phase exceeds the persisted maximum")
            if not self.tools.expand(attempt.attempt_id, target):
                return
            if phase.version > current.phase_version:
                attempt.phase_version = phase.version
                attempt.phase_allocation = ToolClaim(
                    profile_version="phase-v1",
                    slots=target.slots,
                    memory_bytes=target.memory_bytes,
                    artifact_bytes=target.artifact_bytes,
                )
                # Commit before sending. Lost replies retain the entitlement and
                # retry the same version, never a second incremental debit.
                await self._save_attempt(attempt, locked=True)
        applied = await self._send_phase_grant(phase)
        if not self._tool_receipt_matches(attempt, applied) or phase not in applied.phase_grants:
            raise QuarantinedExecution("Executor did not confirm the granted phase")

    async def _send_phase_grant(self, phase: ToolPhase) -> ToolState:
        """Resend one saved absolute grant; the executor treats a replay as a no-op."""
        for attempt in range(_PHASE_GRANT_SENDS):
            try:
                return await self.executor.grant_phase(phase)
            except DomainError as error:
                if not error.retryable or attempt + 1 == _PHASE_GRANT_SENDS:
                    raise
            except Exception:
                if attempt + 1 == _PHASE_GRANT_SENDS:
                    raise
            await asyncio.sleep(0.05 * 2**attempt)
        raise AssertionError("unreachable")

    async def tool(
        self, task_id: str, call: ToolCall, model_round: int, branch: str | None = None
    ) -> JsonObject:
        task = self.get(task_id)
        generation = task.cancel_generation
        spec = (self.chat_registry if task.specification.mode == "chat" else self.registry).require(
            call.name
        )
        spec.check(call)
        prefix = branch_agent_id(task_id, branch) if branch else task_id
        canonical = tool_step_id(prefix, model_round, call.call_id)
        step_id = self.get(task_id).tool_step_aliases.get(canonical, canonical)
        request_payload = {"tool_name": call.name, "arguments": call.arguments}
        attempt = await self._attempt(
            task_id, step_id, "tool", request_payload, model_round, branch=branch
        )
        if attempt.outcome == Outcome.SUCCEEDED and attempt.result is not None:
            return attempt.result
        recovering = attempt.outcome == Outcome.UNKNOWN or not attempt.quiescent
        need_approval = call.name == "run_command"
        if call.name == "read_file":
            path = local_path(call.arguments["path"], self.workspace_root)
            need_approval = not readable(
                path, self.workspace_root, (self.directory / "tasks").resolve()
            )
        if need_approval and not await self._approve_tool(task_id, call, attempt):
            attempt.result = {
                "files": 0,
                "approved": False,
                "message": "用户拒绝了本次操作。",
                "input_version": task.input_version,
            }
            attempt.outcome = Outcome.CANCELLED
            attempt.quiescent = True
            attempt.settled = True
            attempt.state = WorkState.FINISHED
            await self._save_attempt(attempt)
            return attempt.result
        demand, request = self._prepare_tool(attempt, call)
        attempt.state = WorkState.WAIT_RESOURCE
        await self._save_attempt(attempt)

        async def execute() -> JsonObject:
            if self.get(task_id).cancel_requested:
                raise asyncio.CancelledError
            if (
                self.artifact_size() + demand.artifact_bytes
                > self.config.artifact_budget_mib * 1048576
            ):
                raise DomainError("ARTIFACT_BUDGET", "Retained artifacts fill the local budget")
            attempt.state = WorkState.RUNNING
            attempt.quiescent = False
            try:
                await self._save_attempt(attempt)
                if recovering:
                    state = await self.executor.query(attempt.attempt_id)
                    if state is None:
                        raise DomainError("TOOL_UNKNOWN", "Original tool execution is unconfirmed")
                    if state.request != request:
                        raise QuarantinedExecution("Recovered tool request differs")
                else:
                    state = await self.executor.run(request)
                attempt.execution_ref = {
                    "pid": state.pid,
                    "cgroup_path": state.cgroup_path,
                    "attempt_id": attempt.attempt_id,
                }
                await self._save_attempt(attempt)
                while not state.quiescent and state.outcome == Outcome.PENDING:
                    await self._advance_tool_phase(attempt, state, generation)
                    await asyncio.sleep(0.05)
                    found = await self.executor.query(attempt.attempt_id)
                    if found is None:
                        raise DomainError("TOOL_UNKNOWN", "Executor lost the attempt")
                    state = found
                state = await self._settle_tool(attempt, state)
                if not state.quiescent:
                    raise QuarantinedExecution("Tool resources remain unconfirmed")
                if state.outcome != Outcome.SUCCEEDED or state.result is None:
                    if (
                        task.specification.mode == "chat"
                        and state.outcome == Outcome.FAILED
                        and state.result is not None
                    ):
                        return state.result
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
                if error.code in (
                    "EXECUTOR_FULL",
                    "INPUT_VERSION_MISMATCH",
                    "ATTEMPT_CONFLICT",
                    "RESOURCE_PROTOCOL_UNSUPPORTED",
                    "EXECUTOR_NOT_READY",
                ):
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

        remaining = (
            datetime.fromisoformat(self.get(task_id).deadline_at) - datetime.now(UTC)
        ).total_seconds()
        try:
            if not self._tool_maximum(demand, request).fits(self.tools.pool.capacity):
                raise DomainError("RESOURCE_TOO_LARGE", "Tool maximum exceeds total local capacity")
            async with asyncio.timeout(max(0, remaining)):
                return await self.tools.submit(
                    attempt.attempt_id,
                    task_id,
                    demand,
                    execute,
                    priority=self.get(task_id).specification.priority,
                    maximum=self._tool_maximum(demand, request),
                )
        except (TimeoutError, DomainError) as error:
            code = error.code if isinstance(error, DomainError) else "DEADLINE_EXPIRED"
            current = self.attempts[attempt.attempt_id].model_copy(deep=True)
            if current.quiescent and not current.settled and code != "RESERVATION_CONFLICT":
                current.outcome = Outcome.FAILED
                current.state = WorkState.FINISHED
                current.settled = True
                current.error = code
                await self._save_attempt(current)
            if isinstance(error, DomainError):
                raise
            raise DomainError(
                "DEADLINE_EXPIRED", "Tool resource wait or execution exceeded task deadline"
            ) from None

    def _chat_history(self, task: TaskState) -> list[JsonObject]:
        if task.specification.mode != "chat":
            return []
        previous = sorted(
            (
                t
                for t in self.tasks.values()
                if t.conversation_id == task.conversation_id
                and t.task_id != task.task_id
                and t.outcome == Outcome.SUCCEEDED
                and t.created_at < task.created_at
            ),
            key=lambda t: t.created_at,
        )[-8:]
        history: list[JsonObject] = []
        for turn in previous:
            text = turn.specification.message or turn.specification.instruction
            paths = {
                name: str(self.directory / "tasks" / turn.task_id / "inputs" / name)
                for name in turn.specification.documents
            }
            paths.update(
                {
                    image.name: str(
                        self.directory / "tasks" / turn.task_id / "attachments" / image.name
                    )
                    for image in turn.specification.images
                }
            )
            if paths:
                text += "\n附件路径：" + json.dumps(paths, ensure_ascii=False)
            history.extend(
                [
                    {"role": "user", "content": text},
                    {
                        "role": "assistant",
                        "content": (turn.result or {}).get(
                            "content", (turn.result or {}).get("summary", "")
                        ),
                    },
                ]
            )
        return history

    async def _approve_tool(self, task_id: str, call: ToolCall, attempt: AttemptState) -> bool:
        key = attempt.attempt_id
        existing = await self.store.get("approval", key)
        if existing is not None and existing["arguments"] != call.arguments:
            raise DomainError("APPROVAL_CONFLICT", "审批内容与原工具调用不一致")
        if existing is not None and existing["status"] in ("APPROVED", "REJECTED"):
            async with self.lock:
                task = self.get(task_id)
                task.pending_approval = existing
                await self._save_task(task)
            return bool(existing["status"] == "APPROVED")
        if call.name == "run_command":
            cwd = local_path(call.arguments["cwd"], self.workspace_root)
            if not cwd.is_relative_to(self.workspace_root) or not cwd.is_dir():
                raise DomainError("INVALID_TOOL", "指令工作目录必须位于工作区内")
            detail = {"command": call.arguments["command"], "cwd": str(cwd)}
        else:
            detail = {"path": str(local_path(call.arguments["path"], self.workspace_root))}
        waiter: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.approval_waiters[key] = waiter
        try:
            async with self.lock:
                task = self.get(task_id)
                if task.cancel_requested:
                    raise asyncio.CancelledError
                approval = {
                    "approval_id": key,
                    "task_id": task_id,
                    "tool": call.name,
                    "arguments": call.arguments,
                    "generation": task.cancel_generation,
                    "status": "PENDING",
                    **detail,
                }
                task.pending_approval = approval
                task.state = WorkState.WAIT_APPROVAL
                await self._save_task(
                    task, [StoredRecord("approval", key, task.tenant_id, approval)]
                )
            remaining = (
                datetime.fromisoformat(task.deadline_at) - datetime.now(UTC)
            ).total_seconds()
            try:
                return await asyncio.wait_for(waiter, max(0, remaining))
            except TimeoutError:
                async with self.lock:
                    task = self.get(task_id)
                    if task.pending_approval:
                        task.pending_approval["status"] = "EXPIRED"
                        await self._save_task(
                            task,
                            [StoredRecord("approval", key, task.tenant_id, task.pending_approval)],
                        )
                raise DomainError("DEADLINE_EXPIRED", "等待操作确认超时") from None
        finally:
            self.approval_waiters.pop(key, None)

    async def decide_approval(
        self, task_id: str, approval_id: str, allowed: bool, generation: int
    ) -> TaskState:
        async with self.lock:
            task = self.get(task_id)
            approval = task.pending_approval
            if (
                task.cancel_requested
                or task.outcome != Outcome.PENDING
                or task.cancel_generation != generation
            ):
                raise DomainError("APPROVAL_CONFLICT", "任务状态已改变，本次审批已失效")
            if (
                not approval
                or approval["approval_id"] != approval_id
                or approval["status"] != "PENDING"
            ):
                raise DomainError("APPROVAL_CONFLICT", "该审批已处理或不存在")
            approval["status"] = "APPROVED" if allowed else "REJECTED"
            task.state = WorkState.RUNNING
            await self._save_task(
                task, [StoredRecord("approval", approval_id, task.tenant_id, approval)]
            )
            waiter = self.approval_waiters.get(approval_id)
            if waiter is not None and not waiter.done():
                waiter.set_result(allowed)
            return self.get(task_id)

    def _tool_receipt_matches(self, attempt: AttemptState, state: ToolState) -> bool:
        request = state.request
        if (
            request.attempt_id != attempt.attempt_id
            or request.root_task_id != attempt.root_task_id
            or request.tenant_id != attempt.tenant_id
            or request.generation != attempt.generation
            or request.input_version != self.get(attempt.root_task_id).input_version
        ):
            return False
        saved = attempt.request_payload
        if saved is None:
            return True
        if "attempt_id" in saved:
            return request == ToolRun.model_validate(saved)
        return request.tool_name == saved.get("tool_name") and request.arguments == saved.get(
            "arguments"
        )

    async def _settle_tool(self, attempt: AttemptState, state: ToolState) -> ToolState:
        async with self.lock:
            current = self.attempts.get(attempt.attempt_id)
            if current is not None and current.settled:
                # The runner and reconciler can observe the same finish concurrently.
                # Return the durable terminal receipt rather than regress its outcome.
                return state.model_copy(
                    update={
                        "outcome": current.outcome,
                        "quiescent": current.quiescent,
                        "result": current.result,
                        "error": current.error,
                    }
                )
            if not self._tool_receipt_matches(attempt, state):
                raise QuarantinedExecution("Executor receipt differs from the saved tool contract")
            task = self.get(attempt.root_task_id)
            if (
                task.specification.mode == "chat"
                and state.quiescent
                and state.outcome == Outcome.FAILED
            ):
                state = state.model_copy(
                    update={
                        "result": {
                            "error": state.error or "工具执行失败",
                            "files": 0,
                            "input_version": task.input_version,
                        }
                    }
                )
            attempt.quiescent = state.quiescent
            attempt.outcome = state.outcome
            attempt.result = state.result
            attempt.error = state.error
            attempt.state = WorkState.FINISHED if state.quiescent else WorkState.RECONCILING
            attempt.settled = state.quiescent and state.outcome in {
                Outcome.SUCCEEDED,
                Outcome.FAILED,
                Outcome.CANCELLED,
            }
            await self._save_attempt(attempt, locked=True)
            return state

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
                    if attempt.attempt_id in self.tools.active:
                        continue
                    state = await self.executor.query(attempt.attempt_id)
                    if state is not None and state.quiescent:
                        await self._settle_tool(attempt, state)
                        self.tools.pool.release(attempt.attempt_id)
                        self.tools.quarantined.discard(attempt.attempt_id)
                        self.tools.wake.set()
            except Exception:
                continue

        self._retire_model_roots()

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
            self.platform_error = None

    async def refresh_platform(self) -> PlatformFeedback:
        # 先作废缓存再经来源读，调度器与对账循环因此共用同一份快照，
        # 不会一个读到旧的、一个读到新的。
        self.platform_feedback_source.invalidate()
        feedback = await self.platform_feedback_source()
        await self.bind_platform(feedback)
        return feedback

    async def wait_platform(self, request: ModelRequest) -> None:
        """Compatibility wait entry using the same priced admission decision as L3."""
        while True:
            feedback = await self.refresh_platform()
            decision = request_decision(feedback, request=request)
            if decision.action == "refuse":
                raise DomainError("ACCOUNT_BUDGET", "Platform account budget exhausted")
            if not self.config.coordinated or (feedback.can_submit and decision.action == "submit"):
                return
            if datetime.now(UTC) >= datetime.fromisoformat(request.deadline_at):
                raise DomainError(
                    "DEADLINE_EXPIRED", "Task deadline expired while waiting for platform capacity"
                )
            await asyncio.sleep(
                max(0.1, min(feedback.retry_after_seconds, self.config.feedback_interval_seconds))
            )

    def platform_status(self) -> JsonObject:
        # The workbench and scheduler use the same feedback source and observation time.
        feedback = self.platform_feedback_source.latest
        return {
            "platform_url": self.config.platform_url,
            "connected": feedback is not None and self.platform_error is None,
            "coordinated": self.config.coordinated,
            "feedback": feedback.model_dump(mode="json") if feedback else None,
            "error": self.platform_error,
            "feedback_fresh": self.platform_feedback_source.cached is not None,
            "scheduler": {
                "queued": self.models.pending,
                "in_flight": self.models.in_flight,
                "max_inflight": self.config.max_inflight,
            },
        }

    def resources(self) -> JsonObject:
        # 模型侧由 ModelScheduler 自己管在途名额，这里仍按 Resources 的
        # 对外形状汇报容量与可用量，前端与验收脚本不用改。
        model_capacity = asdict(Resources(slots=self.config.max_inflight))
        return {
            "capacity": asdict(self.tools.pool.capacity),
            "available": asdict(self.tools.pool.available),
            "queued_tools": len(self.tools.queue),
            "banker": self.tools.pool.snapshot(),
            "tool_protection": self.tools.snapshot(),
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

    async def tool(self, call: ToolCall, model_round: int) -> JsonObject:
        return await self.service.tool(self.task_id, call, model_round)

    async def checkpoint(self, checkpoint: Checkpoint) -> None:
        async with self.service.lock:
            task = self.service.get(self.task_id)
            if task.cancel_generation != self.generation or task.cancel_requested:
                raise asyncio.CancelledError
            task.checkpoint = checkpoint.model_copy(deep=True)
            await self.service._save_task(task)


class BranchHost:
    """AgentHost adapter for one controlled sub-agent.

    Steps and the checkpoint are scoped to the branch while every model request
    keeps ``root_task_id`` on the root task, so budget and service share never
    grow with the branch count. ``branch`` is the task-local label.
    """

    def __init__(self, service: ClientService, task_id: str, branch: str, generation: int) -> None:
        self.service = service
        self.task_id = task_id
        self.branch = branch
        self.generation = generation

    async def model(self, messages: list[JsonObject], round_index: int) -> ModelReply:
        return await self.service.model(self.task_id, messages, round_index, branch=self.branch)

    async def tool(self, call: ToolCall, model_round: int) -> JsonObject:
        return await self.service.tool(self.task_id, call, model_round, branch=self.branch)

    async def checkpoint(self, checkpoint: Checkpoint) -> None:
        async with self.service.lock:
            task = self.service.get(self.task_id)
            if task.cancel_generation != self.generation or task.cancel_requested:
                raise asyncio.CancelledError
            task.branches[self.branch].checkpoint = checkpoint.model_copy(deep=True)
            await self.service._save_task(task)
