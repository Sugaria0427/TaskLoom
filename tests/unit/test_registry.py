import pytest

from taskloom.contracts.models import ToolCall
from taskloom.domain.model import DomainError
from taskloom.runtime.registry import ToolRegistry, ToolSpec, default_registry

EXPECTED_FILE_STATS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "file_stats",
        "description": ("Read the task's staged files and return verified file/word statistics."),
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
}


def test_default_registry_keeps_baseline_schema() -> None:
    registry = default_registry()
    assert registry.names() == ("file_stats", "grep")
    assert registry.schemas()[0] == EXPECTED_FILE_STATS_SCHEMA
    assert registry.schemas()[1]["function"]["name"] == "grep"
    assert registry.schemas()[1]["function"]["parameters"]["properties"]["pattern"] == {
        "type": "string",
        "description": "Substring to search for",
    }


def test_default_file_stats_rejects_arguments() -> None:
    registry = default_registry()
    spec = registry.require("file_stats")
    with pytest.raises(DomainError, match="no arguments"):
        spec.check(ToolCall(call_id="c1", name="file_stats", arguments={"path": "/etc"}))
    spec.check(ToolCall(call_id="c1", name="file_stats", arguments={}))


def test_duplicate_registration_is_rejected() -> None:
    registry = default_registry()
    duplicate = ToolSpec(
        name="file_stats",
        description="second copy",
        parameters={},
    )
    with pytest.raises(DomainError, match="already registered"):
        registry.register(duplicate)
    assert len(registry) == 2


def test_registry_capacity_is_bounded() -> None:
    registry = ToolRegistry(capacity=2)
    registry.register(ToolSpec(name="a", description="", parameters={}))
    registry.register(ToolSpec(name="b", description="", parameters={}))
    with pytest.raises(DomainError, match="capacity"):
        registry.register(ToolSpec(name="c", description="", parameters={}))
    assert len(registry) == 2


def test_unknown_tool_is_rejected() -> None:
    registry = default_registry()
    with pytest.raises(DomainError, match="unsupported tool"):
        registry.require("rm_rf")
    assert registry.get("rm_rf") is None


def test_parameterized_tool_allows_arguments_and_runs_validate_hook() -> None:
    checked: list[str] = []
    registry = ToolRegistry()

    def validate(call: ToolCall) -> None:
        if "pattern" not in call.arguments:
            raise DomainError("INVALID_TOOL", "missing required argument: pattern")
        checked.append(call.arguments["pattern"])

    spec = ToolSpec(
        name="grep",
        description="Search staged files.",
        parameters={"pattern": {"type": "string"}},
        validate=validate,
    )
    registry.register(spec)
    assert "grep" in registry
    call = ToolCall(call_id="c1", name="grep", arguments={"pattern": "deadlock"})
    registry.require("grep").check(call)
    assert checked == ["deadlock"]

    bad = ToolCall(call_id="c2", name="grep", arguments={})
    with pytest.raises(DomainError, match="missing required argument"):
        registry.require("grep").check(bad)


def test_schema_surface_is_stable_and_ordered() -> None:
    registry = ToolRegistry()
    registry.register(ToolSpec(name="first", description="1", parameters={}))
    registry.register(
        ToolSpec(
            name="second",
            description="2",
            parameters={"limit": {"type": "integer"}},
        )
    )
    names = [item["function"]["name"] for item in registry.schemas()]
    assert names == ["first", "second"]
    assert registry.schemas()[1]["function"]["parameters"] == {
        "type": "object",
        "properties": {"limit": {"type": "integer"}},
        "additionalProperties": False,
    }
