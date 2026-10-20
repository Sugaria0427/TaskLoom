import asyncio
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from taskloom.adapters.executor import ProcessExecutor
from taskloom.adapters.linux import CgroupDomain, OwnerLock, atomic_json, executor_socket
from taskloom.adapters.provider import HTTPProvider
from taskloom.adapters.sqlite import SQLiteRepository, migrate_legacy_database
from taskloom.adapters.transport import ExecutorHTTP, PlatformHTTP
from taskloom.client.service import ClientService
from taskloom.contracts.models import (
    AccountCredential,
    AccountLogin,
    ClientConfig,
    ConversationCreate,
    ConversationState,
    ConversationUpdate,
    GatewayRecord,
    JsonObject,
    ModelRequest,
    PlatformConfig,
    PlatformConnection,
    PlatformFeedback,
    PlatformLogin,
    PoolConfig,
    PriorityUpdate,
    RootRegistration,
    TaskCreate,
    TaskState,
    ToolRun,
    ToolState,
    digest,
    mock_tokens,
)
from taskloom.domain.model import DomainError
from taskloom.domain.ports import StoredRecord
from taskloom.execution.ports import ToolBackend
from taskloom.gateway.service import GatewayService


def application(name: str, lifespan: Any) -> FastAPI:
    app = FastAPI(title="TaskLoom " + name, version="1", lifespan=lifespan)

    @app.exception_handler(httpx.HTTPError)
    async def connection_error(_request: Request, _error: httpx.HTTPError) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "code": "PLATFORM_UNAVAILABLE",
                    "message": "Platform connection failed",
                    "retryable": True,
                }
            },
            status_code=503,
        )

    @app.exception_handler(DomainError)
    async def domain_error(_request: Request, error: DomainError) -> JSONResponse:
        code = 429 if error.retryable else 400
        if error.code == "NOT_FOUND":
            code = 404
        elif error.code in ("UNAUTHORIZED",):
            code = 401
        elif error.code == "ROOT_NOT_REGISTERED":
            code = 403
        elif "CONFLICT" in error.code:
            code = 409
        return JSONResponse(
            {
                "error": {
                    "code": error.code,
                    "message": str(error),
                    "retryable": error.retryable,
                    "retry_after": 1 if error.retryable else None,
                }
            },
            status_code=code,
            headers={"Retry-After": "1"} if error.retryable else None,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, error: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "code": "INVALID_INPUT",
                    "message": "; ".join(
                        str(item["loc"]) + ": " + item["msg"] for item in error.errors()
                    ),
                    "retryable": False,
                }
            },
            status_code=422,
        )

    @app.middleware("http")
    async def bound_body(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 1048576:
                return JSONResponse(
                    {"error": {"code": "BODY_TOO_LARGE", "message": "Maximum body is 1 MiB"}},
                    status_code=413,
                )
        request._body = bytes(body)
        return await call_next(request)

    @app.get("/health/live")
    async def live() -> JsonObject:
        return {"alive": True, "service": name}

    return app


def platform_app(base: Path, config: PlatformConfig) -> FastAPI:
    if (base / "gateway/state.db").exists():
        legacy_owner = OwnerLock(base / "gateway/owner.lock")
        try:
            migrate_legacy_database(base / "gateway/state.db", base / "platform/state.db")
        finally:
            legacy_owner.close()
    store = SQLiteRepository(base / "platform/state.db")
    provider = HTTPProvider(config.pool_url, config.model, config.pool_key, config.pool_kind)
    service = GatewayService(store, provider, config)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        lock = OwnerLock(base / "platform/owner.lock")
        try:
            await service.start()
            yield
        finally:
            await service.close()
            await store.close()
            lock.close()

    app = application("platform", lifespan)
    app.state.service = service

    def tenant(authorization: Annotated[str | None, Header()] = None) -> str:
        token = authorization.removeprefix("Bearer ") if authorization else ""
        return service.accounts.authenticate(token)

    @app.post("/v1/accounts/register", status_code=201)
    async def signup(body: AccountLogin) -> AccountCredential:
        return await service.accounts.register(body)

    @app.post("/v1/accounts/login")
    async def login(body: AccountLogin) -> AccountCredential:
        return await service.accounts.login(body)

    @app.get("/v1/account")
    async def account(user: Annotated[str, Depends(tenant)]) -> PlatformFeedback:
        return await service.feedback(user)

    @app.post("/v1/account/keys/{key_id}/revoke")
    async def revoke(key_id: str, user: Annotated[str, Depends(tenant)]) -> JsonObject:
        await service.accounts.revoke(user, key_id)
        return {"revoked": True}

    @app.get("/v1/models")
    async def models(user: Annotated[str, Depends(tenant)]) -> JsonObject:
        return {"account_id": user, "models": [config.model]}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        return JSONResponse({"ready": service.ready}, status_code=200 if service.ready else 503)

    @app.get("/api/v1/status")
    async def status() -> JsonObject:
        return service.status()

    @app.post("/v1/roots", status_code=202)
    async def root(body: RootRegistration, user: Annotated[str, Depends(tenant)]) -> JsonObject:
        await service.register_root(user, body)
        return {"root_task_id": body.root_task_id}

    @app.post("/v1/requests", status_code=202)
    async def submit(body: ModelRequest, user: Annotated[str, Depends(tenant)]) -> GatewayRecord:
        return await service.submit(user, body)

    @app.get("/v1/requests/{request_id}")
    async def query(request_id: str, user: Annotated[str, Depends(tenant)]) -> GatewayRecord:
        return service.get(user, request_id)

    @app.post("/v1/requests/{request_id}/cancel")
    async def cancel(request_id: str, user: Annotated[str, Depends(tenant)]) -> GatewayRecord:
        return await service.cancel(user, request_id)

    return app


class CancelTool(BaseModel):
    generation: int = Field(ge=0)


def executor_app(base: Path, config: ClientConfig) -> FastAPI:
    profile = base / "clients" / config.profile
    group_root = os.environ.get("TASKLOOM_CGROUP_ROOT")
    cgroups = CgroupDomain(Path(group_root) if group_root else None)
    backend: ToolBackend = ProcessExecutor(profile, cgroups, config.tool_slots)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        lock = OwnerLock(profile / "executor.lock")
        try:
            await backend.start()
            yield
        finally:
            await backend.close()
            lock.close()

    app = application("executor", lifespan)

    @app.get("/health/ready")
    async def ready() -> JsonObject:
        return {"ready": True, "protection": "cgroup-v2" if group_root else "process-group"}

    @app.post("/execute", status_code=202)
    async def execute(body: ToolRun) -> ToolState:
        return await backend.run(body)

    @app.get("/attempts/{attempt_id}")
    async def query(attempt_id: str) -> ToolState:
        found = await backend.get(attempt_id)
        if found is None:
            raise DomainError("NOT_FOUND", "Tool attempt not found")
        return found

    @app.post("/attempts/{attempt_id}/cancel")
    async def cancel(attempt_id: str, body: CancelTool) -> JsonObject:
        if not attempt_id.isalnum() or len(attempt_id) > 96:
            raise DomainError("INVALID_ID", "Invalid attempt identity")
        return {"quiescent": await backend.cancel(attempt_id, body.generation)}

    return app


def client_app(base: Path, config: ClientConfig) -> FastAPI:
    profile = base / "clients" / config.profile
    store = SQLiteRepository(profile / "client.db")
    executor = ExecutorHTTP(str(executor_socket(profile)))
    credentials = profile / "platform-credentials.json"
    connection = (
        PlatformConnection.model_validate_json(credentials.read_text())
        if credentials.exists()
        else None
    )
    if connection is not None:
        config.platform_url = connection.platform_url
    gateway = PlatformHTTP(
        config.platform_url,
        connection.api_key if connection else os.environ.get("TASKLOOM_API_KEY", ""),
    )
    service = ClientService(store, gateway, executor, profile, config)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        lock = OwnerLock(profile / "client.lock")
        try:
            await service.start()
            yield
        finally:
            await service.close()
            await store.close()
            lock.close()

    app = application("client", lifespan)
    app.state.service = service

    async def connect_platform(body: PlatformConnection) -> JsonObject:
        if service.runners or any(not task.quiescent for task in service.tasks.values()):
            raise DomainError(
                "CONNECTION_BUSY",
                "Finish or cancel active work before updating the platform connection",
            )
        candidate = PlatformHTTP(body.platform_url, body.api_key)
        adopted = False
        try:
            feedback = await candidate.feedback()
            await service.bind_platform(feedback)
            atomic_json(credentials, body.model_dump())
            previous = gateway.client
            gateway.client = candidate.client
            gateway.url = body.platform_url
            config.platform_url = body.platform_url
            adopted = True
            await previous.aclose()
            return service.platform_status()
        finally:
            if not adopted:
                await candidate.close()

    @app.get("/api/v1/platform")
    async def platform_status() -> JsonObject:
        return service.platform_status()

    @app.post("/api/v1/platform/connect")
    async def connect(body: PlatformConnection) -> JsonObject:
        return await connect_platform(body)

    @app.post("/api/v1/platform/{action}")
    async def platform_login(action: str, body: PlatformLogin) -> JsonObject:
        if action not in ("register", "login"):
            raise DomainError("NOT_FOUND", "Unknown platform account action")
        if service.runners or any(not task.quiescent for task in service.tasks.values()):
            raise DomainError(
                "CONNECTION_BUSY", "Finish active work before issuing another account key"
            )
        temporary = PlatformHTTP(body.platform_url)
        try:
            login = AccountLogin(username=body.username, password=body.password)
            issued = await (
                temporary.signup(login) if action == "register" else temporary.login(login)
            )
        finally:
            await temporary.close()
        return await connect_platform(
            PlatformConnection(platform_url=body.platform_url, api_key=issued.api_key)
        )

    @app.middleware("http")
    async def local_control(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method == "POST" and request.headers.get("X-TaskLoom-Local") != "1":
            return JSONResponse({"error": {"code": "LOCAL_HEADER_REQUIRED"}}, status_code=403)
        return await call_next(request)

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        return JSONResponse({"ready": service.ready}, status_code=200 if service.ready else 503)

    @app.get("/api/v1/status")
    async def status() -> JsonObject:
        return {
            "ready": service.ready,
            "tenant_id": config.tenant_id,
            "tasks": len(service.tasks),
            "platform": service.platform_status(),
            "protection": "cgroup-v2"
            if os.environ.get("TASKLOOM_CGROUP_ROOT")
            else "process-group",
        }

    @app.post("/api/v1/tasks", status_code=202)
    async def create(body: TaskCreate) -> TaskState:
        return await service.create(body)

    @app.get("/api/v1/conversations")
    async def conversations() -> list[JsonObject]:
        return service.conversation_summaries()

    @app.post("/api/v1/conversations", status_code=201)
    async def create_conversation(body: ConversationCreate) -> ConversationState:
        return await service.create_conversation(body.title)

    @app.get("/api/v1/conversations/{conversation_id}")
    async def conversation(conversation_id: str) -> JsonObject:
        return {
            "conversation": service.conversation(conversation_id).model_dump(mode="json"),
            "tasks": [
                task.model_dump(mode="json")
                for task in service.tasks.values()
                if task.conversation_id == conversation_id
            ],
        }

    @app.post("/api/v1/conversations/{conversation_id}/update")
    async def update_conversation(
        conversation_id: str, body: ConversationUpdate
    ) -> ConversationState:
        return await service.update_conversation(conversation_id, body)

    @app.post("/api/v1/conversations/{conversation_id}/messages", status_code=202)
    async def message(conversation_id: str, body: TaskCreate) -> TaskState:
        return await service.create(body, conversation_id)

    @app.get("/api/v1/tasks")
    async def tasks() -> list[TaskState]:
        return [service.get(task_id) for task_id in reversed(service.tasks)]

    @app.get("/api/v1/tasks/{task_id}")
    async def query(task_id: str) -> TaskState:
        return service.get(task_id)

    @app.get("/api/v1/tasks/{task_id}/graph")
    async def graph(task_id: str) -> JsonObject:
        task = service.get(task_id)
        agents = [
            row
            for row in await store.records("agent", task.tenant_id)
            if row["root_task_id"] == task_id
        ]
        steps = [
            row
            for row in await store.records("step", task.tenant_id)
            if row["root_task_id"] == task_id
        ]
        attempts = [
            row.model_dump(mode="json")
            for row in service.attempts.values()
            if row.root_task_id == task_id
        ]
        return {"task_id": task_id, "agents": agents, "steps": steps, "attempts": attempts}

    @app.post("/api/v1/tasks/{task_id}/cancel")
    async def cancel(task_id: str) -> TaskState:
        return await service.cancel(task_id)

    @app.post("/api/v1/tasks/{task_id}/priority")
    async def update_priority(task_id: str, body: PriorityUpdate) -> TaskState:
        return await service.update_priority(task_id, body.priority)

    @app.post("/api/v1/tasks/{task_id}/resume")
    async def resume(task_id: str) -> TaskState:
        return await service.resume(task_id)

    @app.get("/api/v1/resources")
    async def resources() -> JsonObject:
        return service.resources()

    @app.get("/api/v1/events")
    async def events(after: int = Query(default=0, ge=0)) -> list[JsonObject]:
        return await store.events(config.tenant_id, after)

    @app.get("/api/v1/artifacts/{task_id}")
    async def artifact(task_id: str) -> FileResponse:
        task = service.get(task_id)
        path = profile / "tasks" / task_id / "summary.json"
        if task.result is None or not path.is_file():
            raise DomainError("NOT_FOUND", "No verified summary has been published")
        return FileResponse(path, media_type="application/json", filename="summary.json")

    web = files("taskloom").joinpath("web")
    app.mount("/static", StaticFiles(directory=str(web)), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(str(web.joinpath("index.html")), headers={"Cache-Control": "no-store"})

    return app


class ChatInput(BaseModel):
    model: str
    messages: list[JsonObject]
    tools: list[JsonObject] = Field(default_factory=list)
    max_tokens: int = Field(default=512, ge=1, le=4096)


def pool_app(base: Path, config: PoolConfig) -> FastAPI:
    if not config.key:
        raise RuntimeError("A private model-pool key is required")
    if (base / "provider/state.db").exists():
        legacy_owner = OwnerLock(base / "provider/owner.lock")
        try:
            migrate_legacy_database(base / "provider/state.db", base / "platform/pool.db")
        finally:
            legacy_owner.close()
    store = SQLiteRepository(base / "platform/pool.db")
    locks: dict[str, asyncio.Lock] = {}
    active = 0
    peak = 0
    completed = 0

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        lock = OwnerLock(base / "platform/pool.lock")
        try:
            await store.records("mock")
            yield
        finally:
            await store.close()
            lock.close()

    app = application("model-pool", lifespan)

    def pool_access(authorization: Annotated[str | None, Header()] = None) -> None:
        import hmac

        if not authorization or not hmac.compare_digest(authorization, "Bearer " + config.key):
            raise DomainError("UNAUTHORIZED", "Only the platform can access its model pool")

    @app.get("/health/ready")
    async def ready() -> JsonObject:
        return {
            "ready": True,
            "simulated": True,
            "capacity": config.capacity,
            "model": config.model,
        }

    @app.get("/api/v1/status", dependencies=[Depends(pool_access)])
    async def pool_status() -> JsonObject:
        return {
            "capacity": config.capacity,
            "active": active,
            "peak": peak,
            "completed": completed,
            "model": config.model,
        }

    @app.get("/requests/{request_id}", dependencies=[Depends(pool_access)])
    async def query(request_id: str) -> JsonObject:
        result = await store.get("mock", request_id)
        if result is None:
            raise DomainError("NOT_FOUND", "Fixture request not found")
        return result

    @app.post("/v1/chat/completions", dependencies=[Depends(pool_access)])
    async def completion(
        body: ChatInput, request_id: Annotated[str, Header(alias="X-TaskLoom-Request-Id")]
    ) -> JsonObject:
        nonlocal active, peak, completed
        if body.model != config.model:
            raise DomainError("MODEL_UNAVAILABLE", "The platform pool does not serve this model")
        if active >= config.capacity:
            raise DomainError("POOL_BUSY", "The fixed model pool is full", True)
        active += 1
        peak = max(peak, active)
        try:
            return await generate(body, request_id)
        finally:
            active -= 1
            completed += 1

    async def generate(body: ChatInput, request_id: str) -> JsonObject:
        if len(locks) >= 2048 and request_id not in locks:
            raise DomainError("QUEUE_FULL", "Fixture history capacity exhausted", True)
        lock = locks.setdefault(request_id, asyncio.Lock())
        async with lock:
            previous = await store.get("mock", request_id)
            hashed = digest(body.model_dump())
            if previous:
                if previous["payload_hash"] != hashed:
                    raise DomainError("DUPLICATE_ID_CONFLICT", "Fixture request content changed")
                if previous.get("response"):
                    return previous["response"]  # type: ignore[no-any-return]
            await store.apply(
                [
                    StoredRecord(
                        "mock",
                        request_id,
                        "fixture",
                        {
                            "payload_hash": hashed,
                            "response": None,
                        },
                    )
                ]
            )
            await asyncio.sleep(config.latency_seconds)
            tool_messages = [message for message in body.messages if message.get("role") == "tool"]
            if tool_messages:
                result = json.loads(tool_messages[-1]["content"])
                message: JsonObject = {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "files": result["files"],
                            "words": result["words"],
                            "summary": f"已核对 {result['files']} 份文件，共 {result['words']} 个词。",
                        },
                        ensure_ascii=False,
                    ),
                }
            else:
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_" + request_id[:16],
                            "type": "function",
                            "function": {"name": "file_stats", "arguments": "{}"},
                        }
                    ],
                }
            output_tokens = mock_tokens(message)
            if output_tokens > body.max_tokens:
                raise DomainError("OUTPUT_LIMIT", "Fixture output exceeds the selected output cap")
            response: JsonObject = {
                "id": request_id,
                "choices": [{"message": message, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": mock_tokens({"messages": body.messages, "tools": body.tools}),
                    "completion_tokens": output_tokens,
                },
                "fixture": True,
            }
            await store.apply(
                [
                    StoredRecord(
                        "mock",
                        request_id,
                        "fixture",
                        {
                            "payload_hash": hashed,
                            "response": response,
                        },
                    )
                ]
            )
            return response

    return app
