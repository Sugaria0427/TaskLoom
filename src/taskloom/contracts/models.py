import json
import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from taskloom.domain.identity import digest as digest
from taskloom.domain.identity import identity as identity
from taskloom.domain.identity import utc_now as utc_now
from taskloom.domain.model import Outcome, WorkState

JsonObject = dict[str, Any]
MAX_MODEL_TOOLS = 8


def tool_step_id(task_id: str, model_round: int, call_id: str) -> str:
    """Stable tool-step identity scoped to the model round that produced it."""
    return f"{task_id}-model-{model_round}-tool-{call_id}"


def mock_tokens(value: object) -> int:
    """The fixture contract explicitly defines its own deterministic token unit."""
    return max(1, (len(json.dumps(value, ensure_ascii=False).encode()) + 3) // 4)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolCall(Contract):
    call_id: str
    name: str
    arguments: JsonObject = Field(default_factory=dict)


class Usage(Contract):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_units: int = Field(ge=0)
    unit: str = "mock_microcredit"


class ModelReply(Contract):
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage


class RootRegistration(Contract):
    root_task_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,96}$")
    budget_units: int = Field(default=100000, ge=1, le=10000000)


class ModelRequest(Contract):
    schema_version: Literal[1] = 1
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,96}$")
    root_task_id: str
    agent_id: str
    step_id: str
    attempt_id: str
    cancel_generation: int = Field(default=0, ge=0)
    messages: list[JsonObject] = Field(min_length=1, max_length=32)
    tools: list[JsonObject] = Field(default_factory=list, max_length=MAX_MODEL_TOOLS)
    max_tokens: int = Field(default=512, ge=1, le=4096)
    deadline_at: str

    @field_validator("deadline_at")
    @classmethod
    def aware_deadline(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("Deadline must include a timezone")
        return value

    @property
    def input_tokens(self) -> int:
        return mock_tokens({"messages": self.messages, "tools": self.tools})

    @property
    def reserve_units(self) -> int:
        return self.input_tokens + self.max_tokens


class GatewayRecord(Contract):
    tenant_id: str
    request: ModelRequest
    payload_hash: str
    pool_request_id: str | None = None
    state: str = "QUEUED"
    outcome: Outcome = Outcome.PENDING
    reply: ModelReply | None = None
    reserved_units: int = 0
    consumed_units: int = 0
    quiescent: bool = False
    cancel_requested: bool = False
    error: str | None = None
    created_at: str = Field(default_factory=utc_now)
    dispatched_at: str | None = None
    completed_at: str | None = None


class QuotaState(Contract):
    consumed_units: int = 0
    reserved_units: int = 0
    inflight: int = 0
    window_start: float = 0
    window_requests: int = 0
    window_tokens: int = 0


class TaskCreate(Contract):
    title: str = Field(default="资料统计与摘要", min_length=1, max_length=120)
    instruction: str = Field(default="", max_length=16000)
    documents: dict[str, str] = Field(
        default_factory=lambda: {"notes.txt": "Operating systems manage processes and resources."},
        min_length=1,
        max_length=16,
    )
    work_iterations: int = Field(default=1, ge=1, le=5000000)
    emit_bytes: int = Field(default=0, ge=0, le=8388608)
    budget_units: int = Field(default=100000, ge=100, le=10000000)
    deadline_seconds: float = Field(default=120, ge=1, le=3600)

    @field_validator("documents")
    @classmethod
    def valid_documents(cls, value: dict[str, str]) -> dict[str, str]:
        if sum(len(content.encode()) for content in value.values()) > 524288:
            raise ValueError("Documents exceed 512 KiB")
        for name in value:
            if not re.fullmatch(r"[^/\\\x00-\x1f]{1,80}", name) or name in {".", ".."}:
                raise ValueError("Use simple document filenames")
        return value


class Checkpoint(Contract):
    messages: list[JsonObject] = Field(default_factory=list)
    pending_tools: list[ToolCall] = Field(default_factory=list)
    completed_tools: dict[str, JsonObject] = Field(default_factory=dict)
    completed_tool_calls: dict[str, ToolCall] = Field(default_factory=dict)
    model_round: int = 0


class TaskState(Contract):
    task_id: str
    conversation_id: str | None = None
    tenant_id: str
    agent_id: str
    specification: TaskCreate
    input_version: str
    state: WorkState = WorkState.READY
    outcome: Outcome = Outcome.PENDING
    checkpoint: Checkpoint = Field(default_factory=Checkpoint)
    step_attempts: dict[str, str] = Field(default_factory=dict)
    tool_step_aliases: dict[str, str] = Field(default_factory=dict)
    cancel_generation: int = 0
    cancel_requested: bool = False
    quiescent: bool = True
    consumed_units: int = 0
    reserved_units: int = 0
    result: JsonObject | None = None
    error: str | None = None
    created_at: str = Field(default_factory=utc_now)
    deadline_at: str
    completed_at: str | None = None


class ConversationCreate(Contract):
    title: str = Field(default="新会话", min_length=1, max_length=120)


class ConversationUpdate(Contract):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    archived: bool | None = None


class ConversationState(Contract):
    conversation_id: str
    tenant_id: str
    title: str
    archived: bool = False
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)


class AttemptState(Contract):
    attempt_id: str
    root_task_id: str
    agent_id: str
    step_id: str
    tenant_id: str
    kind: Literal["model", "tool"]
    generation: int
    state: WorkState = WorkState.READY
    outcome: Outcome = Outcome.PENDING
    quiescent: bool = True
    reserved_units: int = 0
    consumed_units: int = 0
    settled: bool = False
    result: JsonObject | None = None
    request_payload: JsonObject | None = None
    execution_ref: JsonObject | None = None
    error: str | None = None


class ToolRun(Contract):
    attempt_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,96}$")
    root_task_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,96}$")
    tenant_id: str
    generation: int = Field(ge=0)
    tool_name: str = "file_stats"
    arguments: JsonObject = Field(default_factory=dict)
    input_version: str
    work_iterations: int = Field(default=1, ge=1, le=5000000)
    emit_bytes: int = Field(default=0, ge=0, le=8388608)
    memory_bytes: int = Field(default=134217728, ge=16777216)
    timeout_seconds: float = Field(default=60, gt=0, le=3600)


class ToolState(Contract):
    request: ToolRun
    outcome: Outcome = Outcome.PENDING
    quiescent: bool = False
    pid: int | None = None
    start_ticks: str | None = None
    boot_id: str | None = None
    cgroup_path: str | None = None
    result: JsonObject | None = None
    error: str | None = None
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    output_truncated: bool = False
    created_at: str = Field(default_factory=utc_now)


class ClientConfig(Contract):
    profile: str = Field(default="alice", pattern=r"^[a-z0-9_-]{1,24}$")
    tenant_id: str = "alice"
    client_port: int = Field(default=8795, ge=1024, le=65535)
    platform_url: str = "http://127.0.0.1:8796"
    coordinated: bool = True
    feedback_interval_seconds: float = Field(default=1, ge=0.1, le=30)
    max_inflight: int = Field(default=2, ge=1, le=16)
    tool_slots: int = Field(default=2, ge=1, le=16)
    memory_budget_mib: int = Field(default=512, ge=128)
    artifact_budget_mib: int = Field(default=64, ge=1)
    max_steps: int = Field(default=12, ge=3, le=128)


class PlatformConfig(Contract):
    listen_host: str = "127.0.0.1"
    pool_url: str = "http://127.0.0.1:8797"
    pool_kind: Literal["simulated", "fixed-http"] = "simulated"
    pool_key: str = Field(default="", repr=False)
    model: str = "taskloom-fixed-model"
    pool_capacity: int = Field(default=2, ge=1, le=64)
    max_queue: int = Field(default=64, ge=1, le=1024)
    max_tenant_queue: int = Field(default=16, ge=1, le=256)
    max_queue_bytes: int = Field(default=2097152, ge=1024)
    max_history_bytes: int = Field(default=16777216, ge=1024)
    requests_per_window: int = Field(default=120, ge=1)
    tokens_per_window: int = Field(default=100000, ge=1)
    window_seconds: float = Field(default=60, gt=0)
    total_budget_units: int = Field(default=1000000, ge=1)
    account_budget_units: int = Field(default=250000, ge=1)
    account_max_inflight: int = Field(default=1, ge=1, le=64)
    account_requests_per_window: int = Field(default=60, ge=1)
    account_tokens_per_window: int = Field(default=50000, ge=1)


class PoolConfig(Contract):
    capacity: int = Field(default=2, ge=1, le=64)
    latency_seconds: float = Field(default=0.05, ge=0, le=30)
    key: str = Field(default="", repr=False)
    model: str = "taskloom-fixed-model"


class AccountLogin(Contract):
    username: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,31}$")
    password: str = Field(min_length=10, max_length=128, repr=False)


class AccountRecord(Contract):
    account_id: str
    password_hash: str = Field(repr=False)
    budget_units: int = Field(ge=1)
    max_inflight: int = Field(ge=1)
    requests_per_window: int = Field(ge=1)
    tokens_per_window: int = Field(ge=1)
    created_at: str = Field(default_factory=utc_now)


class APIKeyRecord(Contract):
    key_id: str
    account_id: str
    digest: str = Field(repr=False)
    revoked: bool = False
    created_at: str = Field(default_factory=utc_now)


class AccountCredential(Contract):
    account_id: str
    key_id: str
    api_key: str = Field(repr=False)


class PlatformConnection(Contract):
    platform_url: str
    api_key: str = Field(min_length=1, max_length=256, repr=False)

    @field_validator("platform_url")
    @classmethod
    def valid_platform_url(cls, value: str) -> str:
        from urllib.parse import urlsplit

        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
            raise ValueError("Use a platform HTTP(S) URL without embedded credentials")
        return value.rstrip("/")


class PlatformLogin(AccountLogin):
    platform_url: str = "http://127.0.0.1:8796"

    @field_validator("platform_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        return PlatformConnection.valid_platform_url(value)


class PlatformFeedback(Contract):
    platform_id: str
    account_id: str
    model: str
    pool_kind: Literal["simulated", "fixed-http"] = "simulated"
    usage_unit: str = "mock_microcredit"
    pool_capacity: int
    pool_inflight: int
    platform_queued: int
    platform_remaining_units: int
    account_budget_units: int
    account_consumed_units: int
    account_reserved_units: int
    account_remaining_units: int
    account_inflight: int
    account_max_inflight: int
    account_queued: int
    account_window_tokens_remaining: int
    can_submit: bool
    retry_after_seconds: float
    recommended_pending_limit: int = 1
    observed_at: str = Field(default_factory=utc_now)
