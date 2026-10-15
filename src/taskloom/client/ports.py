from typing import Protocol

from taskloom.contracts.models import (
    GatewayRecord,
    ModelRequest,
    PlatformFeedback,
    RootRegistration,
    ToolRun,
    ToolState,
)


class GatewayPort(Protocol):
    async def feedback(self) -> PlatformFeedback: ...

    async def register(self, root: RootRegistration) -> None: ...

    async def submit(self, request: ModelRequest) -> GatewayRecord: ...

    async def query(self, request_id: str) -> GatewayRecord: ...

    async def cancel(self, request_id: str) -> GatewayRecord: ...

    async def close(self) -> None: ...


class ExecutorPort(Protocol):
    async def run(self, request: ToolRun) -> ToolState: ...

    async def query(self, attempt_id: str) -> ToolState | None: ...

    async def cancel(self, attempt_id: str, generation: int) -> bool: ...

    async def close(self) -> None: ...
