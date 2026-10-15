import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from taskloom.adapters.sqlite import SQLiteRepository, migrate_legacy_database
from taskloom.bootstrap.apps import platform_app, pool_app
from taskloom.client.ports import ExecutorPort, GatewayPort
from taskloom.client.service import ClientService
from taskloom.contracts.models import ClientConfig, ModelRequest, PlatformConfig, PoolConfig
from taskloom.domain.model import DomainError
from taskloom.domain.ports import StoredRecord


async def test_platform_account_key_lifecycle_and_restart(tmp_path) -> None:
    app = platform_app(tmp_path, PlatformConfig())
    login = {"username": "alice", "password": "correct-password-123"}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            issued = await http.post("/v1/accounts/register", json=login)
            assert issued.status_code == 201
            first = issued.json()
            headers = {"Authorization": "Bearer " + first["api_key"]}
            account = await http.get("/v1/account", headers=headers)
            assert account.json()["account_id"] == "alice"
            platform_id = account.json()["platform_id"]
            assert "password" not in account.text and "api_key" not in account.text
            duplicate = await http.post("/v1/accounts/register", json=login)
            assert duplicate.status_code == 409
            wrong = await http.post(
                "/v1/accounts/login", json={**login, "password": "incorrect-password"}
            )
            assert wrong.status_code == 401
            elevated = await http.post(
                "/v1/accounts/register",
                json={**login, "username": "mallory", "budget_units": 99999999},
            )
            assert elevated.status_code == 422
            second = (await http.post("/v1/accounts/login", json=login)).json()
            assert second["api_key"] != first["api_key"]
            await http.post("/v1/account/keys/" + first["key_id"] + "/revoke", headers=headers)
            assert (await http.get("/v1/account", headers=headers)).status_code == 401
    recovered = platform_app(tmp_path, PlatformConfig())
    async with recovered.router.lifespan_context(recovered):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=recovered), base_url="http://test"
        ) as http:
            feedback = await http.get(
                "/v1/account", headers={"Authorization": "Bearer " + second["api_key"]}
            )
            assert feedback.status_code == 200
            assert feedback.json()["platform_id"] == platform_id
            assert (await http.get("/v1/account", headers=headers)).status_code == 401
            rows = await recovered.state.service.store.records("account")
            assert rows[0]["password_hash"] != login["password"]
            keys = await recovered.state.service.store.records("api_key")
            assert first["api_key"] not in str(keys) and second["api_key"] not in str(keys)


async def test_fixed_pool_requires_internal_key_and_enforces_capacity(tmp_path) -> None:
    app = pool_app(tmp_path, PoolConfig(key="private-pool-secret", capacity=1, latency_seconds=0.2))
    body = {"model": "taskloom-fixed-model", "messages": [{"role": "user", "content": "analyze"}]}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://pool"
        ) as http:
            denied = await http.post(
                "/v1/chat/completions",
                json=body,
                headers={
                    "X-TaskLoom-Request-Id": "one",
                    "Authorization": "Bearer user-account-key",
                },
            )
            assert denied.status_code == 401
            headers = {
                "X-TaskLoom-Request-Id": "one",
                "Authorization": "Bearer private-pool-secret",
            }
            first = asyncio.create_task(
                http.post("/v1/chat/completions", json=body, headers=headers)
            )
            for _ in range(100):
                status = (await http.get("/api/v1/status", headers=headers)).json()
                if status["active"] == 1:
                    break
                await asyncio.sleep(0.005)
            else:
                raise AssertionError("The first pool request did not start")
            busy = await http.post(
                "/v1/chat/completions",
                json=body,
                headers={**headers, "X-TaskLoom-Request-Id": "two"},
            )
            assert busy.status_code == 429
            assert (await first).status_code == 200
            status = (await http.get("/api/v1/status", headers=headers)).json()
            assert status["active"] == 0 and status["peak"] == status["capacity"] == 1


async def test_personal_l3_waits_for_platform_feedback_and_pins_account(
    tmp_path, platform_feedback
) -> None:
    store = SQLiteRepository(tmp_path / "client.db")
    gateway = AsyncMock(spec=GatewayPort)
    executor = AsyncMock(spec=ExecutorPort)
    observed = asyncio.Event()
    current = platform_feedback.model_copy(update={"can_submit": False, "account_queued": 1})

    async def feedback():
        observed.set()
        return current

    gateway.feedback.side_effect = feedback
    service = ClientService(
        store, gateway, executor, tmp_path, ClientConfig(feedback_interval_seconds=0.1)
    )
    request = ModelRequest(
        request_id="one",
        root_task_id="root",
        agent_id="agent",
        step_id="step",
        attempt_id="attempt",
        messages=[{"role": "user", "content": "hello"}],
        deadline_at=(datetime.now(UTC) + timedelta(seconds=3)).isoformat(),
    )
    operation = asyncio.create_task(service.wait_platform(request))
    try:
        await asyncio.wait_for(observed.wait(), 1)
        assert not operation.done()
        current = platform_feedback
        await asyncio.wait_for(operation, 2)
        with pytest.raises(DomainError, match="another profile"):
            await service.bind_platform(platform_feedback.model_copy(update={"account_id": "bob"}))
        binding = await store.get("profile", "platform-account")
        assert binding["account_id"] == "alice"
    finally:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
        await service.close()
        await store.close()


async def test_legacy_ledger_copy_retains_original_and_is_idempotent(tmp_path) -> None:
    source = tmp_path / "gateway/state.db"
    original = SQLiteRepository(source)
    await original.apply([StoredRecord("request", "alice:old", "alice", {"consumed_units": 17})])
    await original.close()
    target = tmp_path / "platform/state.db"
    migrate_legacy_database(source, target)
    assert source.exists() and target.exists()
    copied = SQLiteRepository(target)
    try:
        assert (await copied.get("request", "alice:old"))["consumed_units"] == 17
        await copied.apply([StoredRecord("request", "alice:new", "alice", {"consumed_units": 20})])
        migrate_legacy_database(source, target)
        assert (await copied.get("request", "alice:new"))["consumed_units"] == 20
    finally:
        await copied.close()


def test_personal_config_excludes_platform_pool_credentials() -> None:
    with pytest.raises(ValidationError):
        ClientConfig.model_validate({"pool_key": "platform-private-secret"})
