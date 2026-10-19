"""Bounded tool registry for the personal agent runtime.

Replaces the hard-coded single-tool loop in the agent runtime with a
registry-driven routing layer (WP2 Phase 1).  Each tool is described by a
:class:`ToolSpec` carrying its name, description, JSON parameter schema and
resource demand.  The registry bounds the number of registered tools and
keeps names unique, so the model can only request tools the host actually
supports.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from taskloom.contracts.models import JsonObject, ToolCall
from taskloom.domain.model import DomainError, Resources


@dataclass(frozen=True)
class ToolSpec:
    """A registered tool contract.

    ``parameters`` uses the OpenAPI/JSON Schema property form; an empty
    mapping means the tool takes no arguments.  ``demand`` is the resource
    footprint charged against the personal tool domain when the tool runs.
    ``validate`` is an optional hook that checks a concrete call before it is
    dispatched (e.g. argument names, versions or allowed values).
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
                    "additionalProperties": False,
                },
            },
        }

    def check(self, call: ToolCall) -> None:
        """Reject a call that does not fit the registered contract."""
        if call.name != self.name:
            raise DomainError("INVALID_TOOL", "Tool call does not match its registered spec")
        if not self.parameters and call.arguments:
            raise DomainError("INVALID_TOOL", f"Tool {self.name!r} takes no arguments")
        if self.validate is not None:
            self.validate(call)


class ToolRegistry:
    """Name-keyed registry with a bounded capacity.

    Registration is the only way a tool becomes reachable by the model; the
    registry therefore acts as the admission boundary between model output and
    host execution.
    """

    def __init__(self, capacity: int = 16) -> None:
        if capacity < 1:
            raise ValueError("Tool registry capacity must be at least one")
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
    """The 0.2 baseline tool set: verified staged-file statistics.

    The schema is byte-for-byte compatible with the previous
    ``FILE_STATS_TOOL`` constant so existing requests and acceptance remain
    unchanged.
    """
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="file_stats",
            description=("Read the task's staged files and return verified file/word statistics."),
            parameters={},
        )
    )
    return registry
