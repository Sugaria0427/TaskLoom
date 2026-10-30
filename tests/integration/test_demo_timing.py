import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from taskloom.adapters.executor import ProcessExecutor
from taskloom.adapters.linux import CgroupDomain
from taskloom.adapters.sqlite import SQLiteRepository
from taskloom.bootstrap.apps import client_app
from taskloom.client.demo import timing_plan
from taskloom.contracts.models import (
    AccountLogin,
    ClientConfig,
    DemoSettings,
    ModelReply,
    ModelRequest,
    PlatformConfig,
    RootRegistration,
    ToolRun,
    Usage,
    digest,
)
from taskloom.domain.model import Outcome
from taskloom.gateway.service import GatewayService


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():  # noqa: ASYNC110 - observe the actual scheduler state
            await asyncio.sleep(0.01)


def request(name, settings):
    return ModelRequest(
        request_id=name,
        root_task_id="root",
        agent_id="agent",
        step_id=name,
        attempt_id=name,
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=10,
        deadline_at=(datetime.now(UTC) + timedelta(seconds=20)).isoformat(),
        demo_timing=timing_plan(settings, "model", 1000),
    )


def provider():
    instance = AsyncMock()
    instance.generate.return_value = ModelReply(
        content="真实结果", usage=Usage(input_tokens=1, output_tokens=1, cost_units=2)
    )
    instance.reconcile.side_effect = AssertionError(
        "A saved demonstration reply must not call the provider again"
    )
    return instance


def test_workload_curve_fixed_curve_and_disabled_compatibility():
    settings = DemoSettings(enabled=True, strength=10)
    assert timing_plan(settings, "read_file", 22 * 1024).target_seconds == 2
    assert timing_plan(settings, "run_command", 100000).target_seconds == 1
    assert timing_plan(settings, "list_files", 1).target_seconds == 1
    assert timing_plan(settings, "read_file", 44 * 1024).target_seconds == 4
    settings.exponent = 2
    assert timing_plan(settings, "read_file", 44 * 1024).target_seconds == 8
    assert timing_plan(settings, "read_file", 10**12).target_seconds == 120
    assert timing_plan(DemoSettings(), "read_file", 22 * 1024) is None
    assert "demo_timing" not in request("old", None).model_dump()


async def test_http_settings_persist_and_new_tasks_pin_profile_settings(tmp_path):
    config = ClientConfig()
    app = client_app(tmp_path, config)
    service = app.state.service
    headers = {"X-TaskLoom-Local": "1"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://local"
        ) as client:
            assert not (await client.get("/api/v1/settings/demo")).json()["enabled"]
            assert (
                await client.post("/api/v1/settings/demo", json={"enabled": True})
            ).status_code == 403
            response = await client.post(
                "/api/v1/settings/demo", headers=headers, json={"enabled": True, "strength": 10}
            )
            assert response.status_code == 200
            assert response.json()["strength"] == 10
            assert (
                await client.post("/api/v1/settings/demo", headers=headers, json={"strength": -1})
            ).status_code == 422
        row = await service.store.get("settings", "demo-timing")
        assert row["enabled"]
        # A task copies the profile settings before any runner can mutate them.
        service.refresh_platform = AsyncMock()
        service._run = AsyncMock()
        service.gateway.register = AsyncMock()
        from taskloom.contracts.models import TaskCreate

        task = await service.create(TaskCreate(mode="chat", message="demo"))
        await service.runners[task.task_id]
        await service.set_demo_settings(DemoSettings(enabled=False))
        assert service.get(task.task_id).demo_settings.enabled
    finally:
        await service.store.close()
        await service.gateway.close()
        await service.executor.close()


@pytest.mark.parametrize("cancel", [False, True])
async def test_tool_padding_occupies_process_and_remains_cancellable(tmp_path, cancel):
    workspace = tmp_path / "tasks/root"
    (workspace / "inputs").mkdir(parents=True)
    documents = {"a.txt": "a" * (22 * 1024)}
    (workspace / "inputs/a.txt").write_text(documents["a.txt"])
    (workspace / "input.json").write_text(
        json.dumps({"input_version": digest(documents), "tenant_id": "alice"})
    )
    executor = ProcessExecutor(tmp_path, CgroupDomain(None))
    await executor.start()
    settings = DemoSettings(enabled=True, strength=10 if cancel else 1)
    plan = timing_plan(settings, "file_stats", 22 * 1024)
    run = ToolRun(
        attempt_id="tool",
        root_task_id="root",
        tenant_id="alice",
        generation=0,
        input_version=digest(documents),
        demo_timing=plan,
    )
    try:
        started = time.monotonic()
        await executor.run(run)
        await asyncio.sleep(0.1)
        assert not (await executor.get("tool")).quiescent
        if cancel:
            assert await executor.cancel("tool", 1)
        await until(lambda: executor.states["tool"].quiescent)
        state = await executor.get("tool")
        assert state.outcome == (Outcome.CANCELLED if cancel else Outcome.SUCCEEDED)
        if not cancel:
            assert time.monotonic() - started >= 0.2
            assert state.result["files"] == 1
            assert state.result["demo_timing"]["target_seconds"] == 0.2
    finally:
        await executor.close()


async def prepare_platform(tmp_path):
    repo = SQLiteRepository(tmp_path / "platform.db")
    upstream = provider()
    config = PlatformConfig(pool_capacity=1, account_max_inflight=1)
    platform = GatewayService(repo, upstream, config)
    await platform.start()
    await platform.accounts.register(AccountLogin(username="alice", password="test-password-123"))
    await platform.register_root("alice", RootRegistration(root_task_id="root"))
    return platform, repo, upstream, config


async def test_platform_hold_blocks_slot_and_cancel_settles_actual_usage_once(tmp_path):
    platform, repo, upstream, config = await prepare_platform(tmp_path)
    settings = DemoSettings(enabled=True, strength=1, model_reference_ms=1000)
    try:
        original = request("one", settings)
        await platform.submit("alice", original)
        await until(lambda: platform.get("alice", "one").demo_reply is not None)
        await platform.submit("alice", request("two", None))
        assert platform.get("alice", "two").state == "QUEUED"
        assert platform.quota.inflight == 1
        await platform.cancel("alice", "one")
        await until(lambda: platform.get("alice", "two").state == "FINISHED")
        first = platform.get("alice", "one")
        assert first.outcome == Outcome.CANCELLED and first.consumed_units == 2
        assert platform.get("alice", "two").reply.content == "真实结果"
        assert platform.quota.inflight == 0 and platform.quota.consumed_units == 4
        await platform.submit("alice", original)
        assert upstream.generate.await_count == 2
    finally:
        await platform.close()
        await repo.close()


async def test_command_timeout_is_independent_of_demonstration_padding(tmp_path):
    workspace = tmp_path / "tasks/root"
    (workspace / "inputs").mkdir(parents=True)
    (workspace / "input.json").write_text(
        json.dumps({"input_version": digest({}), "tenant_id": "alice"})
    )
    executor = ProcessExecutor(tmp_path, CgroupDomain(None), workspace_root=str(tmp_path))
    await executor.start()
    run = ToolRun(
        attempt_id="timeout",
        root_task_id="root",
        tenant_id="alice",
        generation=0,
        input_version=digest({}),
        tool_name="run_command",
        arguments={"command": "sleep 30", "cwd": ".", "timeout_seconds": 1},
        workspace_root=str(tmp_path),
        command_approved=True,
        approved_command_cwd=str(tmp_path),
        timeout_seconds=5,
        demo_timing=timing_plan(DemoSettings(enabled=True, strength=20), "run_command"),
    )
    try:
        started = time.monotonic()
        await executor.run(run)
        await until(lambda: executor.states["timeout"].quiescent)
        state = await executor.get("timeout")
        assert state.outcome == Outcome.FAILED and state.error == "TOOL_TIMEOUT"
        assert time.monotonic() - started < 4
    finally:
        await executor.close()


async def test_platform_restart_finishes_saved_reply_without_rebilling(tmp_path):
    platform, repo, upstream, config = await prepare_platform(tmp_path)
    await platform.submit(
        "alice", request("one", DemoSettings(enabled=True, strength=1, model_reference_ms=500))
    )
    await until(lambda: platform.get("alice", "one").demo_reply is not None)
    await platform.close()
    await repo.close()
    repo = SQLiteRepository(tmp_path / "platform.db")
    recovered = GatewayService(repo, upstream, config)
    try:
        await recovered.start()
        await until(lambda: recovered.get("alice", "one").state == "FINISHED")
        record = recovered.get("alice", "one")
        assert record.reply.content == "真实结果"
        assert upstream.generate.await_count == 1
        upstream.reconcile.assert_not_awaited()
        assert recovered.quota.consumed_units == 2 and recovered.quota.inflight == 0
    finally:
        await recovered.close()
        await repo.close()
