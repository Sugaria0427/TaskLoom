import json

import pytest

from taskloom.contracts.models import (
    Checkpoint,
    JsonObject,
    ModelReply,
    TaskCreate,
    ToolCall,
    Usage,
)
from taskloom.domain.model import DomainError
from taskloom.runtime.agent import run_agent
from taskloom.runtime.registry import ToolRegistry, ToolSpec, default_registry


class FakeHost:
    """Scripted AgentHost double recording checkpoints and tool calls."""

    def __init__(self, replies: list[ModelReply]) -> None:
        self.replies = list(replies)
        self.tool_results: dict[str, JsonObject] = {}
        self.tool_calls: list[ToolCall] = []
        self.checkpoints: list[Checkpoint] = []

    async def model(self, messages: list[JsonObject], round_index: int) -> ModelReply:
        return self.replies.pop(0)

    async def tool(self, call: ToolCall) -> JsonObject:
        self.tool_calls.append(call)
        return self.tool_results[call.call_id]

    async def checkpoint(self, checkpoint: Checkpoint) -> None:
        self.checkpoints.append(checkpoint.model_copy(deep=True))


def usage() -> Usage:
    return Usage(input_tokens=10, output_tokens=10, cost_units=1)


def specification() -> TaskCreate:
    return TaskCreate(
        title="statistics",
        instruction="summarize",
        documents={"a.txt": "hello world"},
    )


def final_reply(files: JsonObject, words: int, summary: str) -> ModelReply:
    return ModelReply(
        content=json.dumps(
            {"files": files, "words": words, "summary": summary}, ensure_ascii=False
        ),
        usage=usage(),
    )


async def test_baseline_file_stats_round_trip_via_registry() -> None:
    files = {"a.txt": 2}
    host = FakeHost(
        [
            ModelReply(
                tool_calls=[ToolCall(call_id="c1", name="file_stats", arguments={})],
                usage=usage(),
            ),
            final_reply(files, 2, "two words"),
        ]
    )
    host.tool_results["c1"] = {"files": files, "words": 2}

    result = await run_agent(
        host, specification(), Checkpoint(), max_steps=12, registry=default_registry()
    )

    assert result["files"] == files
    assert result["words"] == 2
    assert result["input_files"] == ["a.txt"]
    assert [call.name for call in host.tool_calls] == ["file_stats"]
    assert len(host.checkpoints) >= 2


async def test_unregistered_tool_is_rejected_before_execution() -> None:
    host = FakeHost(
        [
            ModelReply(
                tool_calls=[ToolCall(call_id="c1", name="rm_rf", arguments={})],
                usage=usage(),
            )
        ]
    )

    with pytest.raises(DomainError, match="unsupported tool"):
        await run_agent(
            host, specification(), Checkpoint(), max_steps=12, registry=default_registry()
        )
    assert host.tool_calls == []


async def test_parameterized_registered_tool_is_routed() -> None:
    files = {"a.txt": 1}
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="file_stats",
            description="Read the task's staged files and return verified statistics.",
            parameters={},
        )
    )
    registry.register(
        ToolSpec(
            name="grep",
            description="Search staged files for a pattern.",
            parameters={"pattern": {"type": "string"}},
        )
    )

    host = FakeHost(
        [
            ModelReply(
                tool_calls=[ToolCall(call_id="c1", name="grep", arguments={"pattern": "hello"})],
                usage=usage(),
            ),
            final_reply(files, 1, "matched hello"),
        ]
    )
    host.tool_results["c1"] = {"files": files, "words": 1, "matches": 1}

    result = await run_agent(host, specification(), Checkpoint(), 12, registry)

    assert result["summary"] == "matched hello"
    assert [call.name for call in host.tool_calls] == ["grep"]
    assert host.tool_calls[0].arguments == {"pattern": "hello"}


async def test_step_limit_still_applies() -> None:
    host = FakeHost(
        [
            ModelReply(
                tool_calls=[ToolCall(call_id=f"c{i}", name="file_stats", arguments={})],
                usage=usage(),
            )
            for i in range(20)
        ]
    )
    for index in range(20):
        host.tool_results[f"c{index}"] = {"files": {}, "words": 0}

    with pytest.raises(DomainError, match="step limit"):
        await run_agent(
            host, specification(), Checkpoint(), max_steps=3, registry=default_registry()
        )
