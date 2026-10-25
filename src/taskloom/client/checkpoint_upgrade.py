"""Upgrade legacy checkpoint keys while retaining their physical execution identity."""

from collections import defaultdict
from collections.abc import Mapping

from taskloom.contracts.models import (
    AttemptState,
    JsonObject,
    ModelReply,
    TaskState,
    ToolCall,
    tool_step_id,
)
from taskloom.domain.model import DomainError, Outcome


def upgrade_checkpoint(
    original: TaskState, attempts: Mapping[str, AttemptState]
) -> tuple[TaskState, list[AttemptState]]:
    """Derive old-to-new keys from successful, durable model replies.

    Aliases resolve new logical keys to existing Steps; Attempt IDs, executor
    requests and historical Step records keep their original identities. Ambiguous
    history fails before any mutation or new dispatch.
    """
    prefix = original.task_id + "-tool-"
    legacy = {
        step: identity
        for step, identity in original.step_attempts.items()
        if step.startswith(prefix)
    }
    if not legacy:
        return original, []

    def invalid(message: str) -> DomainError:
        return DomainError("CHECKPOINT_UPGRADE_REQUIRED", message)

    calls: dict[str, list[tuple[int, ToolCall]]] = defaultdict(list)
    model_prefix = original.task_id + "-model-"
    for step, identity in original.step_attempts.items():
        suffix = step.removeprefix(model_prefix)
        if not step.startswith(model_prefix) or not suffix.isdecimal():
            continue
        model = attempts.get(identity)
        if model is None or model.kind != "model" or model.outcome != Outcome.SUCCEEDED:
            continue
        if (
            model.step_id != step
            or model.root_task_id != original.task_id
            or model.tenant_id != original.tenant_id
        ):
            raise invalid("Legacy model ownership differs from its task")
        if model.result is None:
            continue
        try:
            reply = ModelReply.model_validate(model.result)
        except ValueError as error:
            raise invalid("Legacy model reply cannot be verified") from error
        for call in reply.tool_calls:
            calls[call.call_id].append((int(suffix), call))

    task = original.model_copy(deep=True)
    changed: list[AttemptState] = []
    resolved: dict[str, tuple[str, ToolCall, AttemptState]] = {}
    for step, identity in legacy.items():
        call_id = step[len(prefix) :]
        candidates = calls.get(call_id, [])
        existing = [
            item
            for item in candidates
            if task.tool_step_aliases.get(tool_step_id(task.task_id, item[0], call_id)) == step
        ]
        if len(existing) == 1:
            candidates = existing
        if len(candidates) != 1:
            raise invalid("Legacy tool call has missing or ambiguous model history")
        model_round, call = candidates[0]
        previous = attempts.get(identity)
        if (
            previous is None
            or previous.kind != "tool"
            or previous.step_id != step
            or previous.root_task_id != task.task_id
            or previous.tenant_id != task.tenant_id
        ):
            raise invalid("Legacy tool ownership cannot be verified")
        payload = {"tool_name": call.name, "arguments": call.arguments}
        if previous.request_payload is not None and any(
            previous.request_payload.get(key) != value for key, value in payload.items()
        ):
            raise invalid("Legacy tool arguments differ from the durable model reply")
        current = previous.model_copy(deep=True)
        if current.request_payload is None:
            current.request_payload = payload
        if current != previous:
            changed.append(current)
        canonical = tool_step_id(task.task_id, model_round, call_id)
        if canonical in task.step_attempts or task.tool_step_aliases.get(canonical, step) != step:
            raise invalid("Legacy tool alias conflicts with an existing step")
        task.tool_step_aliases[canonical] = step
        resolved[call_id] = (canonical, call, current)

    completed: dict[str, JsonObject] = {}
    metadata = dict(task.checkpoint.completed_tool_calls)
    for key, result in task.checkpoint.completed_tools.items():
        if key not in resolved:
            if key not in task.tool_step_aliases and key not in metadata:
                raise invalid("Legacy completed result has no verified tool identity")
            completed[key] = result
            continue
        canonical, call, attempt = resolved[key]
        if attempt.outcome != Outcome.SUCCEEDED or attempt.result != result:
            raise invalid("Legacy completed result differs from its successful attempt")
        if canonical in completed or metadata.get(canonical, call) != call:
            raise invalid("Legacy completed result conflicts with a current checkpoint key")
        completed[canonical] = result
        metadata.pop(key, None)
        metadata[canonical] = call
    task.checkpoint.completed_tools = completed
    task.checkpoint.completed_tool_calls = metadata
    return task, changed
