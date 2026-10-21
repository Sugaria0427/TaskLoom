from typing import Protocol

from taskloom.contracts.models import Checkpoint, JsonObject, ModelReply, ToolCall


class AgentHost(Protocol):
    async def model(self, messages: list[JsonObject], round_index: int) -> ModelReply: ...

    async def tool(self, call: ToolCall, model_round: int) -> JsonObject: ...

    async def checkpoint(self, checkpoint: Checkpoint) -> None: ...
