import httpx

from taskloom.contracts.models import (
    AccountCredential,
    AccountLogin,
    ExecutorCapabilities,
    GatewayRecord,
    ModelRequest,
    PlatformFeedback,
    RootRegistration,
    ToolPhase,
    ToolRun,
    ToolState,
)
from taskloom.domain.model import DomainError


def checked(response: httpx.Response) -> httpx.Response:
    if response.is_error:
        try:
            error = response.json()["error"]
            raise DomainError(error["code"], error["message"], error.get("retryable", False))
        except (KeyError, ValueError):
            response.raise_for_status()
    return response


class PlatformHTTP:
    def __init__(self, url: str, api_key: str = "") -> None:
        self.url = url
        self.client = httpx.AsyncClient(
            base_url=url,
            headers={"Authorization": "Bearer " + api_key} if api_key else {},
            timeout=5,
            trust_env=False,
        )

    async def feedback(self) -> PlatformFeedback:
        response = checked(await self.client.get("/v1/account"))
        return PlatformFeedback.model_validate(response.json())

    async def signup(self, login: AccountLogin) -> AccountCredential:
        response = checked(await self.client.post("/v1/accounts/register", json=login.model_dump()))
        return AccountCredential.model_validate(response.json())

    async def login(self, login: AccountLogin) -> AccountCredential:
        response = checked(await self.client.post("/v1/accounts/login", json=login.model_dump()))
        return AccountCredential.model_validate(response.json())

    async def register(self, root: RootRegistration) -> None:
        checked(await self.client.post("/v1/roots", json=root.model_dump()))

    async def submit(self, request: ModelRequest) -> GatewayRecord:
        response = checked(
            await self.client.post("/v1/requests", json=request.model_dump(mode="json"))
        )
        return GatewayRecord.model_validate(response.json())

    async def query(self, request_id: str) -> GatewayRecord:
        return GatewayRecord.model_validate(
            checked(await self.client.get(f"/v1/requests/{request_id}")).json()
        )

    async def cancel(self, request_id: str) -> GatewayRecord:
        return GatewayRecord.model_validate(
            checked(await self.client.post(f"/v1/requests/{request_id}/cancel")).json()
        )

    async def close(self) -> None:
        await self.client.aclose()


class ExecutorHTTP:
    async def grant_phase(self, grant: ToolPhase) -> ToolState:
        response = checked(await self.client.post("/phases/grant", json=grant.model_dump()))
        return ToolState.model_validate(response.json())

    def __init__(self, socket_path: str) -> None:
        self.client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=socket_path),
            base_url="http://executor",
            timeout=5,
        )

    async def run(self, request: ToolRun) -> ToolState:
        if request.resource_protocol == "phase-v1":
            ready = checked(await self.client.get("/health/ready"))
            capabilities = ExecutorCapabilities.model_validate(ready.json())
            if "phase-v1" not in capabilities.resource_protocols:
                raise DomainError(
                    "RESOURCE_PROTOCOL_UNSUPPORTED", "Executor does not support phase-v1"
                )
            if not capabilities.ready:
                raise DomainError("EXECUTOR_NOT_READY", "Executor is not ready", retryable=True)
        response = checked(
            await self.client.post(
                "/execute", json=request.model_dump(mode="json", exclude_defaults=True)
            )
        )
        return ToolState.model_validate(response.json())

    async def query(self, attempt_id: str) -> ToolState | None:
        response = await self.client.get(f"/attempts/{attempt_id}")
        return (
            None
            if response.status_code == 404
            else ToolState.model_validate(checked(response).json())
        )

    async def cancel(self, attempt_id: str, generation: int) -> bool:
        response = checked(
            await self.client.post(
                f"/attempts/{attempt_id}/cancel", json={"generation": generation}
            )
        )
        return bool(response.json()["quiescent"])

    async def close(self) -> None:
        await self.client.aclose()
