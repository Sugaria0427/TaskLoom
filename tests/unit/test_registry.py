from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from taskloom.contracts.models import MAX_MODEL_TOOLS, ModelRequest, ToolCall
from taskloom.domain.model import DomainError
from taskloom.runtime.registry import ToolRegistry, ToolSpec, default_registry

EXPECTED_FILE_STATS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "file_stats",
        "description": ("Read the task's staged files and return verified file/word statistics."),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
}


def tool(name: str) -> ToolSpec:
    return ToolSpec(name=name, description="", parameters={})


def test_default_registry_schemas_declare_required_arguments() -> None:
    registry = default_registry()
    assert registry.names() == ("file_stats", "grep", "list_files", "top_words")
    assert registry.schemas()[0] == EXPECTED_FILE_STATS_SCHEMA
    grep_parameters = registry.schemas()[1]["function"]["parameters"]
    assert grep_parameters["properties"]["pattern"] == {
        "type": "string",
        "description": "Substring to search for",
    }
    assert grep_parameters["required"] == ["pattern"]
    assert grep_parameters["additionalProperties"] is False


def test_default_file_stats_rejects_arguments() -> None:
    registry = default_registry()
    spec = registry.require("file_stats")
    with pytest.raises(DomainError, match="undeclared argument"):
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
    assert len(registry) == 4


def test_registry_capacity_is_bounded_by_explicit_limit() -> None:
    registry = ToolRegistry(capacity=2)
    registry.register(tool("a"))
    registry.register(tool("b"))
    with pytest.raises(DomainError, match="capacity"):
        registry.register(tool("c"))
    assert len(registry) == 2


def test_registry_uses_the_model_request_tool_limit() -> None:
    registry = ToolRegistry()
    for index in range(MAX_MODEL_TOOLS):
        registry.register(tool(f"tool_{index}"))
    assert len(registry) == MAX_MODEL_TOOLS
    with pytest.raises(DomainError) as error:
        registry.register(tool("one_too_many"))
    assert error.value.code == "TOOL_LIMIT"

    with pytest.raises(DomainError) as construction_error:
        ToolRegistry(capacity=MAX_MODEL_TOOLS + 1)
    assert construction_error.value.code == "TOOL_LIMIT"


def test_model_request_enforces_the_same_tool_limit() -> None:
    with pytest.raises(ValidationError):
        ModelRequest(
            request_id="request",
            root_task_id="root",
            agent_id="agent",
            step_id="step",
            attempt_id="attempt",
            messages=[{"role": "user", "content": "hello"}],
            tools=[{} for _ in range(MAX_MODEL_TOOLS + 1)],
            deadline_at=(datetime.now(UTC) + timedelta(seconds=20)).isoformat(),
        )


def test_unknown_tool_is_rejected() -> None:
    registry = default_registry()
    with pytest.raises(DomainError, match="unsupported tool"):
        registry.require("rm_rf")
    assert registry.get("rm_rf") is None


def test_parameterized_tool_validates_structure_then_runs_validate_hook() -> None:
    checked: list[str] = []
    registry = ToolRegistry()

    def validate(call: ToolCall) -> None:
        if call.arguments["pattern"] != "deadlock":
            raise DomainError("INVALID_TOOL", "unsupported search pattern")
        checked.append(call.arguments["pattern"])

    spec = ToolSpec(
        name="grep",
        description="Search staged files.",
        parameters={"pattern": {"type": "string"}},
        validate=validate,
    )
    registry.register(spec)

    valid = ToolCall(call_id="c1", name="grep", arguments={"pattern": "deadlock"})
    registry.require("grep").check(valid)
    assert checked == ["deadlock"]

    bad_arguments: list[tuple[dict[str, Any], str]] = [
        ({"pattern": 123}, "must be string"),
        ({"pattern": "deadlock", "extra": True}, "undeclared argument"),
        ({}, "missing required argument"),
    ]
    for arguments, message in bad_arguments:
        with pytest.raises(DomainError, match=message):
            registry.require("grep").check(
                ToolCall(call_id="bad", name="grep", arguments=arguments)
            )

    with pytest.raises(DomainError, match="unsupported search pattern"):
        registry.require("grep").check(
            ToolCall(call_id="c2", name="grep", arguments={"pattern": "other"})
        )


def test_booleans_are_not_accepted_as_integers() -> None:
    spec = ToolSpec(
        name="bounded",
        description="",
        parameters={"limit": {"type": "integer"}},
    )
    with pytest.raises(DomainError, match="must be integer"):
        spec.check(ToolCall(call_id="c1", name="bounded", arguments={"limit": True}))
    spec.check(ToolCall(call_id="c2", name="bounded", arguments={"limit": 2}))


def test_schema_surface_is_stable_ordered_and_marks_all_parameters_required() -> None:
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
        "required": ["limit"],
        "additionalProperties": False,
    }
