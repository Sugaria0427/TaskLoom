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


def default_registry() -> ToolRegistry:
    """The current tool set: staged-file statistics plus literal search."""
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="file_stats",
            description=("Read the task's staged files and return verified file/word statistics."),
            parameters={},
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
        )
    )
    return registry
