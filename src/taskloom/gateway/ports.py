from typing import Protocol

from taskloom.contracts.models import ModelReply, ModelRequest


class ProviderRejected(Exception):
    """The pool refused the request before execution; nothing was run or billed."""


class ModelProvider(Protocol):
    # `account` names the upstream pool account chosen by the gateway; None means the pool is
    # configured as a single account.
    async def generate(self, request: ModelRequest, account: str | None = None) -> ModelReply: ...

    async def reconcile(self, request_id: str, account: str | None = None) -> ModelReply | None: ...

    async def close(self) -> None: ...
