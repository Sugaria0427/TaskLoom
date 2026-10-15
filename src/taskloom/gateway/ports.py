from typing import Protocol

from taskloom.contracts.models import ModelReply, ModelRequest


class ModelProvider(Protocol):
    async def generate(self, request: ModelRequest) -> ModelReply: ...

    async def reconcile(self, request_id: str) -> ModelReply | None: ...

    async def close(self) -> None: ...
