import json
import tomllib
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from taskloom.adapters.provider import HTTPProvider
from taskloom.adapters.sqlite import SQLiteRepository
from taskloom.adapters.supervisor import write_configuration
from taskloom.bootstrap.config import load_upstream
from taskloom.bootstrap.development import Topology
from taskloom.contracts.models import (
    AccountLogin,
    ModelRequest,
    PlatformConfig,
    RootRegistration,
    digest,
)
from taskloom.domain.model import DomainError
from taskloom.gateway.service import GatewayService


@pytest.mark.parametrize("base", ["https://example.test", "https://example.test/v1"])
async def test_openai_base_paths_and_thinking_option(base):
    observed = []

    def respond(request):
        observed.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 2},
            },
        )

    provider = HTTPProvider(
        base, "real-model", "test-upstream-secret", kind="fixed-http", thinking="disabled"
    )
    headers = dict(provider.client.headers)
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url=base, transport=httpx.MockTransport(respond), headers=headers
    )
    request = ModelRequest(
        request_id="request",
        root_task_id="root",
        agent_id="agent",
        step_id="step",
        attempt_id="attempt",
        messages=[{"role": "user", "content": "你好"}],
        deadline_at=(datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
        token_count_mode="utf8-bound",
    )
    try:
        reply = await provider.generate(request)
        assert observed[0].url.path == "/v1/chat/completions"
        assert observed[0].headers["authorization"] == "Bearer test-upstream-secret"
        assert json.loads(observed[0].content)["thinking"] == {"type": "disabled"}
        assert reply.usage.unit == "token_unit" and reply.usage.cost_units == 10
        assert request.input_tokens > 8
    finally:
        await provider.close()


def test_private_upstream_is_platform_owned_and_toml_roundtrips(tmp_path):
    path = tmp_path / "private.json"
    path.write_text(
        json.dumps(
            {
                "base_url": "https://example.test",
                "api_key": "test-private-upstream-key",
                "model": "real-model",
            }
        )
    )
    topology = Topology(tmp_path / "state", upstream_config=path)
    config = topology.platform_config()
    assert config.pool_kind == "fixed-http" and config.pool_key == "test-private-upstream-key"
    assert "test-private-upstream-key" not in repr(config)
    assert (
        "test-private-upstream-key" not in topology.client_config("alice", 8795).model_dump_json()
    )
    target = tmp_path / "platform.toml"
    write_configuration(target, config)
    assert PlatformConfig.model_validate(tomllib.loads(target.read_text())) == config
    write_configuration(target, PlatformConfig())
    assert PlatformConfig.model_validate(tomllib.loads(target.read_text())).thinking is None
    path.write_text('{"api_key":"test-private-upstream-key"}')
    with pytest.raises(ValueError, match="missing or invalid") as error:
        load_upstream(path)
    assert "test-private-upstream-key" not in str(error.value)


async def test_real_platform_forces_conservative_admission_and_fixture_hash_stays_compatible(
    tmp_path,
):
    from unittest.mock import AsyncMock

    request = ModelRequest(
        request_id="old-request",
        root_task_id="root",
        agent_id="agent",
        step_id="step",
        attempt_id="attempt",
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=1,
        deadline_at=(datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
    )
    provider = AsyncMock()
    store = SQLiteRepository(tmp_path / "real.db")
    service = GatewayService(
        store, provider, PlatformConfig(pool_kind="fixed-http", total_budget_units=100)
    )
    await service.start()
    try:
        await service.accounts.register(
            AccountLogin(username="alice", password="test-password-123")
        )
        await service.register_root("alice", RootRegistration(root_task_id="root"))
        assert request.reserve_units < 100
        with pytest.raises(DomainError, match="budget domain"):
            await service.submit("alice", request)
        assert not service.requests
        provider.generate.assert_not_awaited()
    finally:
        await service.close()
        await store.close()
    store = SQLiteRepository(tmp_path / "fixture.db")
    service = GatewayService(store, AsyncMock(), PlatformConfig())
    await service.start()
    try:
        await service.accounts.register(
            AccountLogin(username="alice", password="test-password-123")
        )
        await service.register_root("alice", RootRegistration(root_task_id="root"))
        record = await service.submit("alice", request)
        old_payload = request.model_dump(mode="json")
        old_payload.pop("token_count_mode")
        assert record.payload_hash == digest(old_payload)
        assert (await service.submit("alice", request)).payload_hash == record.payload_hash
    finally:
        await service.close()
        await store.close()
