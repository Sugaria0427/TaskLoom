import json

from taskloom.contracts.models import Checkpoint, JsonObject, TaskCreate, tool_step_id
from taskloom.domain.model import DomainError
from taskloom.runtime.ports import AgentHost
from taskloom.runtime.registry import ToolRegistry, default_registry


async def run_agent(
    host: AgentHost,
    task_id: str,
    specification: TaskCreate,
    checkpoint: Checkpoint,
    max_steps: int,
    registry: ToolRegistry | None = None,
) -> JsonObject:
    if registry is None:
        registry = default_registry()
    state = checkpoint.model_copy(deep=True)
    if not state.messages:
        state.messages = [
            {
                "role": "system",
                "content": (
                    "Analyze the staged documents using the available tools ("
                    + ", ".join(registry.names())
                    + "), then return only JSON "
                    "with files, words, and summary. Tools are executed by the host."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "title": specification.title,
                        "instruction": specification.instruction,
                        "files": list(specification.documents),
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        await host.checkpoint(state)
    while state.model_round + len(state.completed_tools) < max_steps:
        if state.pending_tools:
            call = state.pending_tools[0]
            spec = registry.require(call.name)
            spec.check(call)
            # model_round advances immediately after a reply, so pending calls
            # belong to the immediately preceding model round.
            model_round = state.model_round - 1
            step_id = tool_step_id(task_id, model_round, call.call_id)
            result = await host.tool(call, model_round)
            state.completed_tools[step_id] = result
            state.completed_tool_calls[step_id] = call
            state.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.call_id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )
            state.pending_tools.pop(0)
            await host.checkpoint(state)
            continue
        reply = await host.model(state.messages, state.model_round)
        state.model_round += 1
        assistant: JsonObject = {"role": "assistant", "content": reply.content or None}
        if reply.tool_calls:
            assistant["tool_calls"] = [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments),
                    },
                }
                for call in reply.tool_calls
            ]
        state.messages.append(assistant)
        state.pending_tools = reply.tool_calls
        if reply.tool_calls:
            await host.checkpoint(state)
            continue
        try:
            final: JsonObject = json.loads(reply.content)
        except (ValueError, TypeError):
            raise DomainError("INVALID_RESULT", "Expected a structured JSON result") from None
        authoritative = next(
            (
                result
                for key, result in reversed(state.completed_tools.items())
                if key in state.completed_tool_calls
                and state.completed_tool_calls[key].name == "file_stats"
                and "files" in result
                and "words" in result
            ),
            None,
        )
        if authoritative is None:
            raise DomainError("UNVERIFIED_RESULT", "No verified file_stats result is available")
        if (
            final.get("files") != authoritative["files"]
            or final.get("words") != authoritative["words"]
        ):
            raise DomainError(
                "RESULT_MISMATCH", "Model summary disagrees with verified file_stats output"
            )
        final["input_files"] = list(specification.documents)
        await host.checkpoint(state)
        return final
    raise DomainError("STEP_LIMIT", "The bounded Agent loop reached its step limit")
