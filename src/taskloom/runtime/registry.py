"""Bounded tool registry for the personal agent runtime.

The registry is the admission boundary between model-authored tool calls and
host execution.  It bounds the tool surface to the same limit as a model
request, enforces each tool's structural argument contract, and then invokes
any tool-specific semantic validator.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from taskloom.contracts.models import MAX_MODEL_TOOLS, JsonObject, ToolCall
from taskloom.domain.model import DomainError, Resources
from taskloom.execution.catalog import tool_demand


def _matches_json_type(value: object, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "null":
        return value is None
    return False


@dataclass(frozen=True)
class ToolSpec:
    """A registered tool contract.

    ``parameters`` uses the OpenAPI/JSON Schema property form.  Every declared
    property is currently required, matching the repository's minimal tool
    surface.  ``demand`` is the resource footprint charged when the tool runs.
    ``validate`` remains available for semantic checks after structural
    validation, such as allowed values that cannot be expressed by a type.
    """

    name: str
    description: str
    parameters: JsonObject = field(default_factory=dict)
    demand: Resources = Resources(slots=1, memory_bytes=134217728, artifact_bytes=3145728)
    validate: Callable[[ToolCall], None] | None = None

    def schema(self) -> JsonObject:
        """OpenAI-style function schema handed to the model."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters,
                    "required": list(self.parameters),
                    "additionalProperties": False,
                },
            },
        }

    def check(self, call: ToolCall) -> None:
        """Reject a call that does not fit the registered contract."""
        if call.name != self.name:
            raise DomainError("INVALID_TOOL", "Tool call does not match its registered spec")

        declared = set(self.parameters)
        supplied = set(call.arguments)
        unexpected = sorted(supplied - declared)
        if unexpected:
            raise DomainError(
                "INVALID_TOOL",
                f"Tool {self.name!r} received undeclared argument(s): {', '.join(unexpected)}",
            )
        missing = sorted(declared - supplied)
        if missing:
            raise DomainError(
                "INVALID_TOOL",
                f"Tool {self.name!r} is missing required argument(s): {', '.join(missing)}",
            )

        for name, raw_schema in self.parameters.items():
            parameter_schema = raw_schema if isinstance(raw_schema, dict) else None
            expected = parameter_schema.get("type") if parameter_schema else None
            if not isinstance(expected, str) or expected not in {
                "string",
                "integer",
                "number",
                "boolean",
                "object",
                "array",
                "null",
            }:
                raise DomainError(
                    "INVALID_TOOL", f"Tool {self.name!r} has an invalid schema for {name!r}"
                )
            if not _matches_json_type(call.arguments[name], expected):
                raise DomainError(
                    "INVALID_TOOL",
                    f"Argument {name!r} for tool {self.name!r} must be {expected}",
                )

        if self.validate is not None:
            self.validate(call)


class ToolRegistry:
    """Name-keyed registry bounded by the model request tool limit."""

    def __init__(self, capacity: int = MAX_MODEL_TOOLS) -> None:
        if capacity < 1:
            raise ValueError("Tool registry capacity must be at least one")
        if capacity > MAX_MODEL_TOOLS:
            raise DomainError(
                "TOOL_LIMIT",
                f"Tool registry capacity must not exceed the model limit of {MAX_MODEL_TOOLS}",
            )
        self.capacity = capacity
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._tools:
            raise DomainError("DUPLICATE_TOOL", f"Tool {spec.name!r} is already registered")
        if len(self._tools) >= self.capacity:
            raise DomainError("TOOL_LIMIT", "Tool registry capacity exceeded")
        self._tools[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def require(self, name: str) -> ToolSpec:
        spec = self._tools.get(name)
        if spec is None:
            raise DomainError("INVALID_TOOL", "The model requested an unsupported tool")
        return spec

    def schemas(self) -> list[JsonObject]:
        """The full tool surface offered to the model, in registration order."""
        return [spec.schema() for spec in self._tools.values()]

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)


def default_registry(*, staged: bool = False) -> ToolRegistry:
    """The current tool set: staged-file statistics plus literal search."""
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="file_stats",
            description=("Read the task's staged files and return verified file/word statistics."),
            parameters={},
            demand=tool_demand("file_stats"),
        )
    )
    registry.register(
        ToolSpec(
            name="grep",
            description=(
                "Search the task's staged files for a literal substring and return "
                "verified per-line matches with match counts."
            ),
            parameters={"pattern": {"type": "string", "description": "Substring to search for"}},
            demand=tool_demand("grep"),
        )
    )
    registry.register(
        ToolSpec(
            name="list_files",
            description="List staged files with byte sizes and SHA-256 checksums.",
            demand=tool_demand("list_files"),
        )
    )
    registry.register(
        ToolSpec(
            name="top_words",
            description="Count staged words and return the most frequent entries.",
            parameters={
                "top_n": {"type": "integer", "description": "Number of entries, from 1 to 1000"}
            },
            demand=tool_demand("top_words"),
            validate=_validate_top_words,
        )
    )
    if staged:
        registry.register(
            ToolSpec(
                name="staged_top_words",
                description="Count staged words with retained input and two controlled memory expansions.",
                parameters={"top_n": {"type": "integer", "description": "Entries, from 1 to 1000"}},
                demand=tool_demand("staged_top_words"),
                validate=_validate_top_words,
            )
        )
    return registry


def _validate_top_words(call: ToolCall) -> None:
    if not 1 <= call.arguments["top_n"] <= 1000:
        raise DomainError("INVALID_TOOL", "top_n must be between 1 and 1000")


def agent_registry(*, staged: bool = False) -> ToolRegistry:
    registry = default_registry(staged=staged)
    descriptions = {
        "file_stats": "核验当前消息上传的纯文本附件，返回文件数、词数与校验值。仅在需要统计时使用。",
        "grep": "在当前消息的纯文本附件中搜索指定字面内容，返回匹配行。",
        "list_files": "列出当前消息上传的纯文本附件及文件大小。",
        "top_words": "统计当前消息纯文本附件中的高频词，top_n 为 1 到 1000。",
        "staged_top_words": "分阶段统计附件高频词，保留输入并由宿主批准后续内存申请；top_n 为 1 到 1000。",
    }
    # Rebuild descriptions without changing the legacy registry's compatibility surface.
    modern = ToolRegistry()
    for name in registry.names():
        old = registry.require(name)
        modern.register(
            ToolSpec(name, descriptions[name], old.parameters, old.demand, old.validate)
        )
    modern.register(
        ToolSpec(
            "read_file",
            "读取指定路径的本地纯文本或图片；图片会作为视觉消息返回。相对路径基于工作目录；工作区外读取由系统弹出确认卡，请直接调用以申请授权。",
            {
                "path": {
                    "type": "string",
                    "description": "本地绝对路径、Windows 路径或工作区相对路径",
                },
                "max_bytes": {"type": "integer", "description": "文本读取上限，1 到 65536"},
            },
            tool_demand("read_file"),
            _validate_read,
        )
    )
    modern.register(
        ToolSpec(
            "run_command",
            "申请执行一条本地 shell 指令。调用后系统弹出确认卡，用户确认完整指令与工作目录后执行，返回退出码和有界输出。",
            {
                "command": {"type": "string", "description": "需要执行的完整 shell 指令"},
                "cwd": {
                    "type": "string",
                    "description": "工作区内的工作目录，相对路径或绝对路径；通常使用 .",
                },
                "timeout_seconds": {"type": "integer", "description": "超时秒数，1 到 120"},
            },
            tool_demand("run_command"),
            _validate_command,
        )
    )
    return modern


def _validate_read(call: ToolCall) -> None:
    if (
        not call.arguments["path"]
        or len(call.arguments["path"]) > 2048
        or not 1 <= call.arguments["max_bytes"] <= 65536
    ):
        raise DomainError("INVALID_TOOL", "文件路径或读取上限无效")


def _validate_command(call: ToolCall) -> None:
    if not call.arguments["command"].strip() or len(call.arguments["command"]) > 8192:
        raise DomainError("INVALID_TOOL", "指令不能为空，长度上限为 8192")
    if len(call.arguments["cwd"]) > 2048 or not 1 <= call.arguments["timeout_seconds"] <= 120:
        raise DomainError("INVALID_TOOL", "工作目录或超时设置无效")
