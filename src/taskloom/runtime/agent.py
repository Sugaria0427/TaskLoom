import json

from taskloom.contracts.models import Checkpoint, JsonObject, TaskCreate, ToolCall, tool_step_id
from taskloom.domain.model import DomainError
from taskloom.runtime.ports import AgentHost
from taskloom.runtime.prompts import GENERAL_PROMPT, SKILLS
from taskloom.runtime.registry import ToolRegistry, default_registry


async def run_agent(
    host: AgentHost,
    task_id: str,
    specification: TaskCreate,
    checkpoint: Checkpoint,
    max_steps: int,
    registry: ToolRegistry | None = None,
    *,
    history: list[JsonObject] | None = None,
    workspace: str = ".",
    attachments: dict[str, str] | None = None,
) -> JsonObject:
    if registry is None:
        registry = default_registry()
    state = checkpoint.model_copy(deep=True)
    if not state.messages:
        if specification.mode == "chat":
            prompt = GENERAL_PROMPT + "\n当前工作目录：" + workspace
            if specification.skill:
                prompt += "\n\n" + SKILLS[specification.skill]["prompt"]
            text = specification.message or specification.instruction or "请查看附件。"
            if attachments:
                text += "\n\n附件路径：\n" + "\n".join(
                    f"{name}：{path}" for name, path in attachments.items()
                )
            content: object = text
            if specification.images:
                content = [
                    {"type": "text", "text": text},
                    *[
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{image.mime_type};base64,{image.data_base64}"
                            },
                        }
                        for image in specification.images
                    ],
                ]
            state.messages = [
                {"role": "system", "content": prompt},
                *(history or []),
                {"role": "user", "content": content},
            ]
        else:
            # Existing recorded statistics workflows keep their bounded verification contract.
            state.messages = [
                {
                    "role": "system",
                    "content": "请核验附件并返回 JSON，包含 files、words、summary；统计以 file_stats 回执为准。",
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
            model_round = state.model_round - 1
            step_id = tool_step_id(task_id, model_round, call.call_id)
            result = await host.tool(call, model_round)
            state.completed_tools[step_id] = result
            state.completed_tool_calls[step_id] = call
            visible = {key: value for key, value in result.items() if key != "image_data_url"}
            state.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.call_id,
                    "content": json.dumps(visible, ensure_ascii=False),
                }
            )
            state.pending_tools.pop(0)
            if not state.pending_tools:
                existing = [
                    part.get("image_url", {}).get("url")
                    for message in state.messages
                    if isinstance(message.get("content"), list)
                    for part in message["content"]
                    if isinstance(part, dict) and part.get("type") == "image_url"
                ]
                for output in state.completed_tools.values():
                    image_url = output.get("image_data_url")
                    if image_url and image_url not in existing and len(existing) < 4:
                        state.messages.append(
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "文件工具返回的图片："
                                        + str(output.get("path", "")),
                                    },
                                    {"type": "image_url", "image_url": {"url": image_url}},
                                ],
                            }
                        )
                        existing.append(image_url)
            await host.checkpoint(state)
            continue
        reply = await host.model(state.messages, state.model_round)
        calls = list(reply.tool_calls)
        if (
            specification.mode == "chat"
            and specification.skill == "stats"
            and not any(call.name == "file_stats" for call in calls)
            and not any(call.name == "file_stats" for call in state.completed_tool_calls.values())
        ):
            # The selected skill owns this required step; model tool choice is optional.
            call_id = "taskloom_skill_stats"
            while any(call.call_id == call_id for call in calls):
                call_id += "_"
            calls.append(ToolCall(call_id=call_id, name="file_stats"))
        state.model_round += 1
        assistant: JsonObject = {"role": "assistant", "content": reply.content or None}
        if calls:
            assistant["tool_calls"] = [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                }
                for call in calls
            ]
        state.messages.append(assistant)
        state.pending_tools = calls
        if calls:
            await host.checkpoint(state)
            continue
        if specification.mode == "chat":
            result = {"content": reply.content, "summary": reply.content}
            if specification.skill == "stats":
                stats = next(
                    (
                        value
                        for key, value in reversed(state.completed_tools.items())
                        if state.completed_tool_calls.get(key) is not None
                        and state.completed_tool_calls[key].name == "file_stats"
                        and "error" not in value
                        and "files" in value
                        and "words" in value
                    ),
                    None,
                )
                if stats is None:
                    failed = next(
                        (
                            value.get("error", "统计工具未返回完整结果")
                            for key, value in reversed(state.completed_tools.items())
                            if state.completed_tool_calls.get(key) is not None
                            and state.completed_tool_calls[key].name == "file_stats"
                        ),
                        "统计工具未返回结果",
                    )
                    raise DomainError("STATISTICS_FAILED", "附件统计失败：" + str(failed))
                result.update({"files": stats["files"], "words": stats["words"]})
            await host.checkpoint(state)
            return result
        return await _verified_legacy(host, state, specification, reply.content)
    raise DomainError("STEP_LIMIT", "The bounded Agent loop reached its step limit")


async def _verified_legacy(
    host: AgentHost, state: Checkpoint, specification: TaskCreate, content: str
) -> JsonObject:
    try:
        final: JsonObject = json.loads(content)
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
    if final.get("files") != authoritative["files"] or final.get("words") != authoritative["words"]:
        raise DomainError(
            "RESULT_MISMATCH", "Model summary disagrees with verified file_stats output"
        )
    final["input_files"] = list(specification.documents)
    await host.checkpoint(state)
    return final
