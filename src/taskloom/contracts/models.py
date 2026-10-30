import base64
import json
import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from taskloom.domain.identity import digest as digest
from taskloom.domain.identity import identity as identity
from taskloom.domain.identity import utc_now as utc_now
from taskloom.domain.model import Outcome, WorkState
from taskloom.policies.reservation import Prices, worst_units

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
    messages: list[JsonObject] = Field(min_length=1, max_length=64)
    tools: list[JsonObject] = Field(default_factory=list, max_length=MAX_MODEL_TOOLS)
    max_tokens: int = Field(default=512, ge=1, le=4096)
    deadline_at: str
    token_count_mode: Literal["fixture", "utf8-bound"] = "fixture"

    @field_validator("deadline_at")
    @classmethod
    def aware_deadline(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("Deadline must include a timezone")
        return value

    @property
    def input_tokens(self) -> int:
        if self.token_count_mode == "utf8-bound":
            # A byte-level upper estimate plus chat/tool envelope allowance. Actual usage
            # always comes from the provider response, never from this admission estimate.
            images = 0
            messages = []
            for message in self.messages:
                current = dict(message)
                if isinstance(current.get("content"), list):
                    blocks = []
                    for block in current["content"]:
                        if isinstance(block, dict) and block.get("type") == "image_url":
                            images += 1
                            blocks.append({"type": "image_url", "image_url": {"url": "image"}})
                        else:
                            blocks.append(block)
                    current["content"] = blocks
                messages.append(current)
            payload = json.dumps({"messages": messages, "tools": self.tools}, ensure_ascii=False)
            return (
                len(payload.encode())
                + 128
                + 64 * len(messages)
                + 256 * len(self.tools)
                + images * 8192
            )
        return mock_tokens({"messages": self.messages, "tools": self.tools})

    @property
    def reserve_units(self) -> int:
        return self.input_tokens + self.max_tokens


class GatewayRecord(Contract):
    tenant_id: str
    request: ModelRequest
    payload_hash: str
    pool_request_id: str | None = None
    # Upstream pool account chosen at dispatch; None when the pool is a single account.
    upstream_account: str | None = None
    state: str = "QUEUED"
    outcome: Outcome = Outcome.PENDING
    reply: ModelReply | None = None
    # Expected charge while dispatched; feedback and precision report it.
    reserved_units: int = 0
    # Worst-case charge while dispatched; budget admission sums it, so no budget is overrun.
    worst_units: int = 0
    # Tokens charged to the token windows at dispatch; trued up to actual usage at finish.
    window_tokens: int = 0
    consumed_units: int = 0
    quiescent: bool = False
    cancel_requested: bool = False
    error: str | None = None
    created_at: str = Field(default_factory=utc_now)
    dispatched_at: str | None = None
    completed_at: str | None = None
    # Pool refusals before execution so far, and when the next dispatch may be tried.
    dispatch_attempts: int = 0
    not_before: str | None = None


class QuotaState(Contract):
    consumed_units: int = 0
    reserved_units: int = 0
    worst_units: int = 0
    inflight: int = 0
    window_start: float = 0
    window_requests: int = 0
    window_tokens: int = 0


class ImageAttachment(Contract):
    name: str = Field(min_length=1, max_length=80)
    mime_type: Literal["image/png", "image/jpeg", "image/webp", "image/gif"]
    data_base64: str = Field(max_length=524288)

    @model_validator(mode="after")
    def valid_image(self) -> "ImageAttachment":
        if not re.fullmatch(r"[^/\\\x00-\x1f]{1,80}", self.name) or self.name in {".", ".."}:
            raise ValueError("Use a simple attachment filename")
        try:
            data = base64.b64decode(self.data_base64, validate=True)
        except ValueError:
            raise ValueError("Invalid image encoding") from None
        if not data or len(data) > 393216:
            raise ValueError("Image exceeds 384 KiB")
        from taskloom.execution.images import inspect_image

        if inspect_image(data) != self.mime_type:
            raise ValueError("Image bytes differ from their declared format")
        return self


class TaskCreate(Contract):
    title: str = Field(default="资料统计与摘要", min_length=1, max_length=120)
    instruction: str = Field(default="", max_length=16000)
    documents: dict[str, str] = Field(
        default_factory=lambda: {"notes.txt": "Operating systems manage processes and resources."},
        min_length=0,
        max_length=16,
    )
    work_iterations: int = Field(default=1, ge=1, le=5000000)
    emit_bytes: int = Field(default=0, ge=0, le=8388608)
    budget_units: int = Field(default=100000, ge=100, le=10000000)
    priority: Literal["low", "normal", "high"] = "normal"
    deadline_seconds: float = Field(default=120, ge=1, le=3600)
    mode: Literal["legacy", "chat"] = "legacy"
    message: str = Field(default="", max_length=16000)
    skill: Literal["notes", "stats"] | None = None
    images: list[ImageAttachment] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def chat_inputs(self) -> "TaskCreate":
        if self.mode == "chat":
            if "documents" not in self.model_fields_set:
                self.documents = {}
            if not self.message.strip() and not self.documents and not self.images:
                raise ValueError("A message or attachment is required")
        names = [*self.documents, *(image.name for image in self.images)]
        if len(names) != len(set(names)) or len(names) > 16:
            raise ValueError("Attachment names must be unique, with at most 16 files")
        total = sum(len(text.encode()) for text in self.documents.values())
        total += sum(len(base64.b64decode(image.data_base64)) for image in self.images)
        if total > 524288:
            raise ValueError("Attachments exceed 512 KiB")
        return self

    @field_validator("documents")
    @classmethod
    def valid_documents(cls, value: dict[str, str]) -> dict[str, str]:
        if sum(len(content.encode()) for content in value.values()) > 524288:
            raise ValueError("Documents exceed 512 KiB")
        for name in value:
            if not re.fullmatch(r"[^/\\\x00-\x1f]{1,80}", name) or name in {".", ".."}:
                raise ValueError("Use simple document filenames")
        return value


class PriorityUpdate(Contract):
    priority: Literal["low", "normal", "high"]


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
    pending_approval: JsonObject | None = None
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


class ToolClaim(Contract):
    profile_version: str = "catalog-v1"
    slots: int = Field(ge=1)
    memory_bytes: int = Field(ge=0)
    artifact_bytes: int = Field(ge=0)


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
    # Original L3 fairness debit, in the platform's priced cost units.
    fair_charge_units: int | None = Field(default=None, ge=1)
    settled: bool = False
    result: JsonObject | None = None
    request_payload: JsonObject | None = None
    tool_claim: ToolClaim | None = None
    execution_ref: JsonObject | None = None
    error: str | None = None


class ToolRun(Contract):
    attempt_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,96}$")
    root_task_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,96}$")
    tenant_id: str
    generation: int = Field(ge=0)
    tool_name: Literal[
        "file_stats", "grep", "list_files", "top_words", "read_file", "run_command"
    ] = "file_stats"
    input_version: str
    work_iterations: int = Field(default=1, ge=1, le=5000000)
    emit_bytes: int = Field(default=0, ge=0, le=8388608)
    memory_bytes: int = Field(default=134217728, ge=16777216)
    cpu_quota: int = Field(default=100000, ge=1000, le=100000)
    cpu_period: int = Field(default=100000, ge=1000, le=1000000)
    timeout_seconds: float = Field(default=60, gt=0, le=3600)
    arguments: JsonObject = Field(default_factory=dict)
    workspace_root: str | None = None
    approved_external_path: str | None = None
    command_approved: bool = False
    approved_command_cwd: str | None = None


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
    peak_memory_bytes: int | None = None
    cpu_seconds: float | None = None
    artifact_bytes: int | None = None
    oom: bool = False
    # How the attempt takes part in a shared preparation: it runs the tool
    # ("owner"), waits for another attempt without a process ("waiter"), or was
    # served from a published preparation ("shared"); None runs unshared.
    share_role: Literal["owner", "waiter", "shared"] | None = None
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
    workspace_root: str | None = None


class PoolAccount(Contract):
    """One upstream account of the platform's fixed model pool, with its own slots and cache."""

    account_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    capacity: int = Field(default=2, ge=1, le=64)
    # None falls back to PlatformConfig.pool_url / pool_key.
    url: str | None = None
    key: str | None = Field(default=None, repr=False)


SchedulingPolicyName = Literal["rr", "drr", "maxmin", "hybrid"]


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
    # Pool prices relative to one uncached input token = 1 cost unit. The defaults match the
    # simulated pool, which bills prompt + completion tokens one unit each without caching.
    output_price: float = Field(default=1.0, gt=0, le=1000)
    cache_read_price: float = Field(default=1.0, ge=0, le=1000)
    cache_write_price: float = Field(default=1.0, ge=0, le=1000)
    # L4 order: tenant/root round-robin (rr, default until the full comparison is done);
    # max-min of the slots each tenant (and root) holds now, recent slot-seconds breaking
    # ties (hybrid, the candidate); cumulative slot-second fairness (drr); or max-min alone.
    scheduling_policy: SchedulingPolicyName = "rr"
    # Empty: one upstream account from pool_url/pool_key/pool_capacity, as before. Otherwise
    # pool_capacity is the sum of the account capacities (set here when left unspecified).
    pool_accounts: list[PoolAccount] = Field(default_factory=list, max_length=64)
    # How long a request may step aside while the account holding its chain's prompt cache is
    # full, before it is placed on another account (delay scheduling). 0 places at once.
    cache_wait_seconds: float = Field(default=0.0, ge=0, le=600)
    # Bearer key of the scheduling console (/admin/v1/*); empty disables it. Never a tenant key.
    admin_key: str = Field(default="", repr=False)
    thinking: Literal["disabled", "enabled"] | None = None

    @model_validator(mode="after")
    def consistent_pool(self) -> "PlatformConfig":
        if not self.pool_accounts:
            return self
        if len({a.account_id for a in self.pool_accounts}) != len(self.pool_accounts):
            raise ValueError("Pool account ids must be unique")
        total = sum(a.capacity for a in self.pool_accounts)
        if total > 64:
            raise ValueError("Pool accounts exceed 64 slots in total")
        if "pool_capacity" in self.model_fields_set and self.pool_capacity != total:
            raise ValueError("pool_capacity must equal the sum of pool account capacities")
        self.pool_capacity = total
        return self


class PoolConfig(Contract):
    capacity: int = Field(default=2, ge=1, le=64)
    latency_seconds: float = Field(default=0.05, ge=0, le=30)
    key: str = Field(default="", repr=False)
    model: str = "taskloom-fixed-model"


class UpstreamConfig(Contract):
    """Private, platform-owned upstream configuration; never returned to a personal client."""

    base_url: str
    api_key: str = Field(min_length=1, repr=False)
    model: str = Field(min_length=1)
    thinking: Literal["disabled", "enabled"] | None = "disabled"

    @field_validator("base_url")
    @classmethod
    def https_endpoint(cls, value: str) -> str:
        from urllib.parse import urlsplit

        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("The upstream must be an HTTPS URL without embedded credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("The upstream URL must not contain a query or fragment")
        return value.rstrip("/")


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
    # Admission budgets sum worst cases: budget minus consumption minus in-flight worst cases,
    # the smaller of this account and the shared platform budget.
    account_inflight_worst_units: int = 0
    account_admission_headroom_units: int = 0
    output_price: float = 1.0
    cache_read_price: float = 1.0
    cache_write_price: float = 1.0
    # Recent output tokens of this account's requests; None until one has finished.
    expected_output_tokens: int | None = None
    blocked_by: Literal["budget", "window", "queue"] | None = None
    recommended_pending_limit: int = 1
    observed_at: str = Field(default_factory=utc_now)

    def admission_needs(self, request: ModelRequest) -> tuple[int, int]:
        """Worst-case budget units and window tokens L4 admits `request` on."""
        prices = Prices(self.output_price, self.cache_read_price, self.cache_write_price)
        worst = worst_units(request.input_tokens, request.max_tokens, prices)
        return worst, request.reserve_units


class SchedulerSettings(Contract):
    """Console change to the L4 scheduler; a missing field keeps its current value."""

    policy: SchedulingPolicyName | None = None
    cache_wait_seconds: float | None = Field(default=None, ge=0, le=600)

    @model_validator(mode="after")
    def some_change(self) -> "SchedulerSettings":
        if self.policy is None and self.cache_wait_seconds is None:
            raise ValueError("Provide policy or cache_wait_seconds")
        return self


# The console snapshot carries identities, counts, times and states only: never request
# messages, tools or replies.
SchedulerBlock = Literal[
    "backoff",
    "pool",
    "window",
    "budget",
    "account_inflight",
    "account_window",
    "account_budget",
    "turn",
]
SchedulerEventKind = Literal[
    "dispatch", "finish", "reject", "unknown", "expire", "cancel", "settings"
]


class SchedulerPool(Contract):
    model: str
    pool_kind: Literal["simulated", "fixed-http"]
    capacity: int
    inflight: int
    queued: int
    unknown: int


class SchedulerShared(Contract):
    budget_units: int
    consumed_units: int
    reserved_units: int
    worst_units: int
    window_seconds: float
    window_remaining_seconds: float
    requests_per_window: int
    window_requests: int
    tokens_per_window: int
    window_tokens: int


class SchedulerRoot(Contract):
    root_task_id: str
    running: int
    unknown: int = 0
    queued: int
    oldest_wait_seconds: float | None = None


class SchedulerTenant(Contract):
    account_id: str
    running: int
    queued: int
    unknown: int
    max_inflight: int
    budget_units: int
    consumed_units: int
    reserved_units: int
    worst_units: int
    requests_per_window: int
    window_requests: int
    tokens_per_window: int
    window_tokens: int
    oldest_wait_seconds: float | None = None
    blocked_by: SchedulerBlock | None = None
    roots: list[SchedulerRoot] = Field(default_factory=list)


class SchedulerUpstream(Contract):
    account_id: str
    capacity: int
    busy: int


class SchedulerUnknown(Contract):
    account_id: str
    request_id: str
    upstream_account: str | None = None
    dispatched_at: str | None = None
    error: str | None = None


class SchedulerEvent(Contract):
    at: str
    kind: SchedulerEventKind
    account_id: str | None = None
    root_task_id: str | None = None
    request_id: str | None = None
    upstream_account: str | None = None
    units: int | None = None
    detail: str | None = None


class SchedulerSnapshot(Contract):
    at: str
    ready: bool
    policy: SchedulingPolicyName
    policies: list[SchedulingPolicyName]
    policy_changed_at: str | None = None
    cache_wait_seconds: float
    placement: bool
    pool: SchedulerPool
    shared: SchedulerShared
    tenants: list[SchedulerTenant]
    upstream: list[SchedulerUpstream]
    unknown: list[SchedulerUnknown]
    events: list[SchedulerEvent]
