import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
from importlib.resources import files

import httpx

from taskloom.adapters.sqlite import SQLiteRepository
from taskloom.bootstrap.apps import platform_app
from taskloom.contracts.models import (
    AccountLogin,
    ModelReply,
    ModelRequest,
    PlatformConfig,
    PoolAccount,
    RootRegistration,
    SchedulerSettings,
    Usage,
)
from taskloom.gateway.service import GatewayService
from taskloom.policies.scheduling import HybridPolicy, MaxMinPolicy

ADMIN = "admin-console-secret-key"
SECRET_TEXT = "PAYLOAD-MUST-NOT-LEAK-5f8c1e"


class ControlledProvider:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.identities: list[str] = []

    async def generate(self, request: ModelRequest, account: str | None = None) -> ModelReply:
        self.identities.append(request.request_id)
        await self.release.wait()
        return ModelReply(
            content=SECRET_TEXT, usage=Usage(input_tokens=1, output_tokens=1, cost_units=2)
        )

    async def reconcile(self, request_id: str, account: str | None = None) -> ModelReply | None:
        return None

    async def close(self) -> None:
        pass


def request(name: str, root: str = "root") -> ModelRequest:
    return ModelRequest(
        request_id=name,
        root_task_id=root,
        agent_id="agent-" + root,
        step_id=name,
        attempt_id=name,
        messages=[{"role": "user", "content": SECRET_TEXT}],
        deadline_at=(datetime.now(UTC) + timedelta(seconds=20)).isoformat(),
    )


async def until(predicate, deadline_seconds: float = 3) -> None:
    async with asyncio.timeout(deadline_seconds):
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.02)


def admin_headers(key: str = ADMIN) -> dict[str, str]:
    return {"Authorization": "Bearer " + key}


async def started(store: SQLiteRepository, provider, config: PlatformConfig) -> GatewayService:
    service = GatewayService(store, provider, config)
    await service.start()
    for name in ("alice", "bob"):
        if name not in service.accounts.accounts:
            await service.accounts.register(
                AccountLogin(username=name, password="test-password-123")
            )
        await service.register_root(name, RootRegistration(root_task_id="root"))
    return service


async def test_admin_endpoints_require_their_own_key(tmp_path) -> None:
    disabled = platform_app(tmp_path / "disabled", PlatformConfig())
    async with disabled.router.lifespan_context(disabled):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=disabled), base_url="http://test"
        ) as http:
            response = await http.get("/admin/v1/scheduler", headers=admin_headers())
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "ADMIN_DISABLED"
            empty = await http.get("/admin/v1/scheduler", headers=admin_headers(""))
            assert empty.status_code == 403

    app = platform_app(tmp_path / "enabled", PlatformConfig(admin_key=ADMIN))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            missing = await http.get("/admin/v1/scheduler")
            assert missing.status_code == 401
            assert missing.json()["error"]["code"] == "ADMIN_UNAUTHORIZED"
            wrong = await http.get("/admin/v1/scheduler", headers=admin_headers("wrong-key"))
            assert wrong.status_code == 401
            issued = await http.post(
                "/v1/accounts/register",
                json={"username": "alice", "password": "correct-password-123"},
            )
            tenant_key = issued.json()["api_key"]
            as_tenant = await http.get("/admin/v1/scheduler", headers=admin_headers(tenant_key))
            assert as_tenant.status_code == 401
            # Nor does the admin key work as a tenant key.
            assert (await http.get("/v1/account", headers=admin_headers())).status_code == 401
            denied = await http.post(
                "/admin/v1/scheduler/settings",
                json={"policy": "hybrid"},
                headers=admin_headers(tenant_key),
            )
            assert denied.status_code == 401
            ok = await http.get("/admin/v1/scheduler", headers=admin_headers())
            assert ok.status_code == 200
            snapshot = ok.json()
            assert snapshot["policy"] == "rr"
            assert snapshot["policies"] == ["rr", "hybrid", "drr", "maxmin"]
            assert snapshot["placement"] is False and snapshot["upstream"] == []
            assert ADMIN not in ok.text and tenant_key not in ok.text
            for body in (
                {},
                {"policy": None, "cache_wait_seconds": None},
                {"policy": "fifo"},
                {"cache_wait_seconds": 601},
                {"cache_wait_seconds": -1},
                {"policy": "rr", "extra": 1},
            ):
                invalid = await http.post(
                    "/admin/v1/scheduler/settings", json=body, headers=admin_headers()
                )
                assert invalid.status_code == 422, body
            assert app.state.service.policy.name == "rr"
            page = await http.get("/admin")
            assert page.status_code == 200
            assert page.headers["cache-control"] == "no-store"
            index = files("taskloom").joinpath("web/admin/index.html")
            assert page.text == index.read_text(encoding="utf-8")
            assert "/admin/static/admin.js" in page.text
            assert (await http.get("/admin/static/admin.js")).status_code == 200


async def test_snapshot_reports_slots_queues_reasons_and_events_without_content(tmp_path) -> None:
    app = platform_app(tmp_path, PlatformConfig(admin_key=ADMIN, pool_capacity=2))
    provider = ControlledProvider()
    app.state.service.provider = provider
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            keys = {}
            for name in ("alice", "bob"):
                issued = await http.post(
                    "/v1/accounts/register",
                    json={"username": name, "password": "correct-password-123"},
                )
                keys[name] = {"Authorization": "Bearer " + issued.json()["api_key"]}
                await http.post("/v1/roots", json={"root_task_id": "root"}, headers=keys[name])

            async def scheduler() -> dict:
                response = await http.get("/admin/v1/scheduler", headers=admin_headers())
                assert response.status_code == 200
                assert SECRET_TEXT not in response.text
                return response.json()

            for name in ("a1", "a2"):
                submitted = await http.post(
                    "/v1/requests",
                    json=request(name).model_dump(mode="json"),
                    headers=keys["alice"],
                )
                assert submitted.status_code == 202
            await until(lambda: provider.identities != [])
            snapshot = await scheduler()
            alice = snapshot["tenants"][0]
            assert alice["account_id"] == "alice"
            assert (alice["running"], alice["queued"]) == (1, 1)
            # The pool has a free slot; alice's own in-flight limit holds a2 back.
            assert alice["blocked_by"] == "account_inflight"
            assert alice["oldest_wait_seconds"] is not None
            assert alice["roots"] == [
                {
                    "root_task_id": "root",
                    "running": 1,
                    "queued": 1,
                    "oldest_wait_seconds": alice["oldest_wait_seconds"],
                }
            ]
            assert snapshot["pool"]["inflight"] == 1 and snapshot["pool"]["queued"] == 1

            for name in ("b1", "b2"):
                await http.post(
                    "/v1/requests", json=request(name).model_dump(mode="json"), headers=keys["bob"]
                )
            await until(lambda: len(provider.identities) == 2)
            snapshot = await scheduler()
            assert [t["account_id"] for t in snapshot["tenants"]] == ["alice", "bob"]
            assert {t["blocked_by"] for t in snapshot["tenants"]} == {"pool"}
            assert snapshot["pool"] == {
                "model": "taskloom-fixed-model",
                "pool_kind": "simulated",
                "capacity": 2,
                "inflight": 2,
                "queued": 2,
                "unknown": 0,
            }
            assert snapshot["shared"]["window_requests"] == 2
            assert snapshot["shared"]["reserved_units"] > 0
            dispatched = [e for e in snapshot["events"] if e["kind"] == "dispatch"]
            # Newest first, with the expected charge.
            assert [e["request_id"] for e in dispatched] == ["b1", "a1"]
            assert all(e["units"] > 0 for e in dispatched)

            await http.post("/v1/requests/b2/cancel", headers=keys["bob"])
            provider.release.set()
            await until(lambda: app.state.service.status()["queued"] == 0, 5)
            await until(lambda: app.state.service.quota.inflight == 0, 5)
            snapshot = await scheduler()
            kinds = Counter(e["kind"] for e in snapshot["events"])
            assert kinds == {"dispatch": 3, "finish": 3, "cancel": 1}
            cancel = next(e for e in snapshot["events"] if e["kind"] == "cancel")
            assert (cancel["request_id"], cancel["detail"]) == ("b2", "dequeued")
            assert all(e["units"] == 2 for e in snapshot["events"] if e["kind"] == "finish")
            # Ledger-only accounts are still listed, idle and unblocked.
            assert {t["account_id"] for t in snapshot["tenants"]} == {"alice", "bob"}
            assert all(
                (t["running"], t["queued"], t["blocked_by"]) == (0, 0, None)
                for t in snapshot["tenants"]
            )
            assert snapshot["tenants"][0]["consumed_units"] == 4


async def test_policy_hot_switch_keeps_queue_and_counts_running_work(tmp_path) -> None:
    store = SQLiteRepository(tmp_path / "gateway.db")
    provider = ControlledProvider()
    service = await started(store, provider, PlatformConfig(pool_capacity=1, admin_key=ADMIN))
    try:
        names = []
        for tenant in ("alice", "bob"):
            for index in range(3):
                names.append(tenant[0] + str(index))
                await service.submit(tenant, request(names[-1]))
        await until(lambda: len(provider.identities) == 1)
        # The pool sees account-scoped identities, so the runner is read from the records.
        runner = next(r.tenant_id for r in service.requests.values() if r.state != "QUEUED")
        idle = "bob" if runner == "alice" else "alice"
        before = (await service.scheduler_snapshot()).pool.queued
        assert before == 5 and len(service.policy) == 5

        snapshot = await service.update_settings(SchedulerSettings(policy="maxmin"))
        assert snapshot.policy == "maxmin" and snapshot.policy_changed_at is not None
        assert snapshot.pool.queued == 5
        assert isinstance(service.policy, MaxMinPolicy) and len(service.policy) == 5
        assert service.policy.held(runner) == 1 and service.policy.held(idle) == 0

        snapshot = await service.update_settings(SchedulerSettings(policy="hybrid"))
        assert snapshot.pool.queued == 5
        assert isinstance(service.policy, HybridPolicy) and len(service.policy) == 5
        now = service.clock()
        assert service.policy.usage(runner, now) >= 1.0 and service.policy.usage(idle, now) == 0
        assert service.config.scheduling_policy == "hybrid"
        assert [e.detail for e in snapshot.events if e.kind == "settings"] == [
            "policy maxmin -> hybrid",
            "policy rr -> maxmin",
        ]
        # An unchanged setting is a no-op.
        assert len((await service.update_settings(SchedulerSettings(policy="hybrid"))).events) == (
            len(snapshot.events)
        )

        provider.release.set()
        await until(lambda: all(r.state == "FINISHED" for r in service.requests.values()), 5)
        # Every request was dispatched exactly once.
        assert len(provider.identities) == len(set(provider.identities)) == len(names)
        assert len(service.policy) == 0 and service.quota.inflight == 0
    finally:
        await service.close()
        await store.close()


async def test_settings_persist_across_restart_and_reach_placement(tmp_path) -> None:
    path = tmp_path / "gateway.db"
    config = PlatformConfig(
        admin_key=ADMIN,
        pool_accounts=[PoolAccount(account_id="up1", capacity=1), PoolAccount(account_id="up2")],
    )
    store = SQLiteRepository(path)
    service = await started(store, ControlledProvider(), config)
    try:
        snapshot = await service.update_settings(
            SchedulerSettings(policy="hybrid", cache_wait_seconds=5)
        )
        assert snapshot.placement is True
        assert [(u.account_id, u.capacity, u.busy) for u in snapshot.upstream] == [
            ("up1", 1, 0),
            ("up2", 2, 0),
        ]
        assert snapshot.cache_wait_seconds == 5
        assert service.placement is not None and service.placement.wait_seconds == 5
        changed_at = snapshot.policy_changed_at
        # The shared configuration object is replaced, not changed in place.
        assert config.scheduling_policy == "rr" and config.cache_wait_seconds == 0
        waited = await service.update_settings(SchedulerSettings(cache_wait_seconds=2.5))
        assert waited.policy == "hybrid" and waited.policy_changed_at == changed_at
        audit = [e for e in await store.events("_platform", 0) if e["type"] == "scheduler.settings"]
        assert [e["detail"] for e in audit] == [
            "policy rr -> hybrid; cache_wait 0 -> 5",
            "cache_wait 5 -> 2.5",
        ]
    finally:
        await service.close()
        await store.close()

    store = SQLiteRepository(path)
    restarted = await started(store, ControlledProvider(), config)
    try:
        assert isinstance(restarted.policy, HybridPolicy)
        assert restarted.config.scheduling_policy == "hybrid"
        assert restarted.config.cache_wait_seconds == 2.5
        assert restarted.placement is not None and restarted.placement.wait_seconds == 2.5
        snapshot = await restarted.scheduler_snapshot()
        assert snapshot.policy == "hybrid" and snapshot.policy_changed_at == changed_at
        assert restarted.status()["policy"] == "hybrid"
    finally:
        await restarted.close()
        await store.close()
