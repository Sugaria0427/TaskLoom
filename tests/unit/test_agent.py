import json

import pytest

from taskloom.contracts.models import (
    Checkpoint,
    JsonObject,
    ModelReply,
    TaskCreate,
    ToolCall,
    Usage,
    tool_step_id,
)
from taskloom.domain.model import DomainError
from taskloom.runtime.agent import run_agent
from taskloom.runtime.registry import ToolRegistry, ToolSpec, default_registry


class FakeHost:
    """Scripted AgentHost double recording checkpoints and round-scoped calls."""

    def __init__(self, replies: list[ModelReply]) -> None:
        self.replies = list(replies)
        self.tool_results: dict[tuple[int, str], JsonObject] = {}
        self.tool_calls: list[tuple[int, ToolCall]] = []
        self.checkpoints: list[Checkpoint] = []

    async def model(self, messages: list[JsonObject], round_index: int) -> ModelReply:
        return self.replies.pop(0)

    async def tool(self, call: ToolCall, model_round: int) -> JsonObject:
        self.tool_calls.append((model_round, call))
        return self.tool_results[(model_round, call.call_id)]

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
    host.tool_results[(0, "c1")] = {"files": files, "words": 2}

    result = await run_agent(
        host, "root", specification(), Checkpoint(), max_steps=12, registry=default_registry()
    )

    assert result["files"] == files
    assert result["words"] == 2
    assert result["input_files"] == ["a.txt"]
    assert [(round_index, call.name) for round_index, call in host.tool_calls] == [
        (0, "file_stats")
    ]
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
            host, "root", specification(), Checkpoint(), max_steps=12, registry=default_registry()
        )
    assert host.tool_calls == []


async def test_parameterized_registered_tool_is_routed_before_authoritative_stats() -> None:
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
            ModelReply(
                tool_calls=[ToolCall(call_id="c2", name="file_stats", arguments={})],
                usage=usage(),
            ),
            final_reply(files, 1, "matched hello"),
        ]
    )
    host.tool_results[(0, "c1")] = {"files": files, "words": 1, "matches": 1}
    host.tool_results[(1, "c2")] = {"files": files, "words": 1}

    result = await run_agent(host, "root", specification(), Checkpoint(), 12, registry)

    assert result["summary"] == "matched hello"
    assert [(round_index, call.name) for round_index, call in host.tool_calls] == [
        (0, "grep"),
        (1, "file_stats"),
    ]
    assert host.tool_calls[0][1].arguments == {"pattern": "hello"}


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
        host.tool_results[(index, f"c{index}")] = {"files": {}, "words": 0}

    with pytest.raises(DomainError, match="step limit"):
        await run_agent(
            host, "root", specification(), Checkpoint(), max_steps=3, registry=default_registry()
        )


async def test_multi_tool_chain_executes_two_tools_in_order() -> None:
    files = {"a.txt": 2}
    host = FakeHost(
        [
            ModelReply(
                tool_calls=[ToolCall(call_id="c1", name="grep", arguments={"pattern": "hello"})],
                usage=usage(),
            ),
            ModelReply(
                tool_calls=[ToolCall(call_id="c2", name="file_stats", arguments={})],
                usage=usage(),
            ),
            final_reply(files, 5, "grep first, stats second"),
        ]
    )
    host.tool_results[(0, "c1")] = {"files": files, "words": 2, "matches": 1}
    host.tool_results[(1, "c2")] = {"files": files, "words": 5}

    result = await run_agent(
        host, "root", specification(), Checkpoint(), max_steps=12, registry=default_registry()
    )

    assert result["files"] == files
    assert result["words"] == 5
    assert [(round_index, call.name) for round_index, call in host.tool_calls] == [
        (0, "grep"),
        (1, "file_stats"),
    ]
    assert len(host.checkpoints) >= 3


async def test_same_call_id_in_two_model_rounds_has_distinct_completed_keys() -> None:
    files = {"a.txt": 2}
    host = FakeHost(
        [
            ModelReply(
                tool_calls=[ToolCall(call_id="c1", name="file_stats", arguments={})],
                usage=usage(),
            ),
            ModelReply(
                tool_calls=[ToolCall(call_id="c1", name="file_stats", arguments={})],
                usage=usage(),
            ),
            final_reply(files, 3, "used the later statistics"),
        ]
    )
    host.tool_results[(0, "c1")] = {"files": files, "words": 2}
    host.tool_results[(1, "c1")] = {"files": files, "words": 3}

    result = await run_agent(
        host, "root", specification(), Checkpoint(), max_steps=12, registry=default_registry()
    )

    first = tool_step_id("root", 0, "c1")
    second = tool_step_id("root", 1, "c1")
    assert result["words"] == 3
    assert [round_index for round_index, _ in host.tool_calls] == [0, 1]
    assert set(host.checkpoints[-1].completed_tools) == {first, second}
    assert host.checkpoints[-1].completed_tools[first]["words"] == 2
    assert host.checkpoints[-1].completed_tools[second]["words"] == 3


async def test_grep_word_count_cannot_authorize_corpus_total() -> None:
    files = {"a.txt": 3}
    host = FakeHost(
        [
            ModelReply(
                tool_calls=[ToolCall(call_id="c1", name="grep", arguments={"pattern": "missing"})],
                usage=usage(),
            ),
            ModelReply(
                tool_calls=[ToolCall(call_id="c2", name="file_stats", arguments={})],
                usage=usage(),
            ),
            final_reply(files, 0, "incorrectly reused the grep count"),
        ]
    )
    host.tool_results[(0, "c1")] = {"files": files, "words": 0, "matches": 0}
    host.tool_results[(1, "c2")] = {"files": files, "words": 3}

    with pytest.raises(DomainError) as error:
        await run_agent(
            host, "root", specification(), Checkpoint(), max_steps=12, registry=default_registry()
        )

    assert error.value.code == "RESULT_MISMATCH"
    assert [(round_index, call.name) for round_index, call in host.tool_calls] == [
        (0, "grep"),
        (1, "file_stats"),
    ]


async def test_grep_only_result_cannot_authorize_final_summary() -> None:
    files = {"a.txt": 1}
    host = FakeHost(
        [
            ModelReply(
                tool_calls=[ToolCall(call_id="c1", name="grep", arguments={"pattern": "hello"})],
                usage=usage(),
            ),
            final_reply(files, 1, "grep is not corpus statistics"),
        ]
    )
    host.tool_results[(0, "c1")] = {"files": files, "words": 1, "matches": 1}

    with pytest.raises(DomainError) as error:
        await run_agent(
            host, "root", specification(), Checkpoint(), max_steps=12, registry=default_registry()
        )

    assert error.value.code == "UNVERIFIED_RESULT"
