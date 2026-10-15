import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from taskloom.adapters.sqlite import SQLiteRepository
from taskloom.contracts.models import (
    AccountLogin,
    ModelReply,
    ModelRequest,
    PlatformConfig,
    RootRegistration,
    Usage,
)
from taskloom.domain.model import DomainError, Outcome
from taskloom.gateway.service import GatewayService


class ControlledProvider:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.fail = False
        self.receipt = False
        self.roots = []
        self.identities = []

    async def generate(self, request: ModelRequest) -> ModelReply:
        self.calls += 1
        self.roots.append(request.root_task_id)
        self.identities.append(request.request_id)
        self.started.set()
        if self.fail:
            raise TimeoutError("connection lost after dispatch")
        await self.release.wait()
        return self.reply()

    @staticmethod
    def reply() -> ModelReply:
        return ModelReply(
            content="verified", usage=Usage(input_tokens=1, output_tokens=1, cost_units=2)
        )

    async def reconcile(self, request_id: str) -> ModelReply | None:
        return self.reply() if self.receipt else None

    async def close(self) -> None:
        pass


def request(name: str, root: str = "root") -> ModelRequest:
    return ModelRequest(
        request_id=name,
        root_task_id=root,
        agent_id="agent",
        step_id=name,
        attempt_id=name,
        messages=[{"role": "user", "content": "hello"}],
        deadline_at=(datetime.now(UTC) + timedelta(seconds=20)).isoformat(),
    )


async def until(predicate, deadline_seconds: float = 3) -> None:
    async with asyncio.timeout(deadline_seconds):
        # Poll the observable contract rather than the scheduler's own wake event.
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.02)


async def test_duplicate_admission_and_queued_cancel(tmp_path) -> None:
    store = SQLiteRepository(tmp_path / "gateway.db")
    provider = ControlledProvider()
    service = GatewayService(store, provider, PlatformConfig(pool_capacity=1))
    await service.start()
    if "alice" not in service.accounts.accounts:
        await service.accounts.register(
            AccountLogin(username="alice", password="test-password-123")
        )
    try:
        await service.register_root("alice", RootRegistration(root_task_id="root"))
        first = request("one")
        await service.submit("alice", first)
        await provider.started.wait()
        await service.submit("alice", first)
        with pytest.raises(DomainError, match="different content"):
            await service.submit("alice", first.model_copy(update={"max_tokens": 513}))
        await service.submit("alice", request("two"))
        cancelled = await service.cancel("alice", "two")
        assert cancelled.quiescent and cancelled.outcome == Outcome.CANCELLED
        assert provider.calls == 1
        with pytest.raises(DomainError):
            service.get("bob", "one")
        provider.release.set()
        await until(lambda: service.get("alice", "one").state == "FINISHED")
        assert service.quota.consumed_units == 2
        assert service.quota.reserved_units == 0
        assert service.quota.inflight == 0
    finally:
        await service.close()
        await store.close()


async def test_unknown_dispatch_is_reconciled_without_replay(tmp_path) -> None:
    store = SQLiteRepository(tmp_path / "gateway.db")
    provider = ControlledProvider()
    provider.fail = True
    service = GatewayService(store, provider, PlatformConfig())
    await service.start()
    if "alice" not in service.accounts.accounts:
        await service.accounts.register(
            AccountLogin(username="alice", password="test-password-123")
        )
    try:
        await service.register_root("alice", RootRegistration(root_task_id="root"))
        await service.submit("alice", request("uncertain"))
        await until(lambda: service.get("alice", "uncertain").state == "UNKNOWN")
        assert service.quota.reserved_units > 0
        assert service.quota.inflight == 1
        provider.receipt = True
        await service.reconcile()
        await service.reconcile()
        assert service.quota.reserved_units == 0
        assert service.quota.consumed_units == 2
        assert provider.calls == 1
    finally:
        await service.close()
        await store.close()


async def test_gateway_restarts_reconcile_persisted_dispatch_intent(tmp_path) -> None:
    path = tmp_path / "gateway.db"
    provider = ControlledProvider()
    first_store = SQLiteRepository(path)
    service = GatewayService(first_store, provider, PlatformConfig())
    await service.start()
    if "alice" not in service.accounts.accounts:
        await service.accounts.register(
            AccountLogin(username="alice", password="test-password-123")
        )
    await service.register_root("alice", RootRegistration(root_task_id="root"))
    await service.submit("alice", request("restart"))
    await provider.started.wait()
    await service.close()
    await first_store.close()
    provider.receipt = True
    second_store = SQLiteRepository(path)
    recovered = GatewayService(second_store, provider, PlatformConfig())
    await recovered.start()
    try:
        assert recovered.get("alice", "restart").state == "FINISHED"
        assert recovered.quota.consumed_units == 2
        assert (await recovered.feedback("alice")).account_consumed_units == 2
        assert recovered.quota.reserved_units == 0
        assert provider.calls == 1
    finally:
        await recovered.close()
        await second_store.close()


async def test_quota_blocked_request_still_expires(tmp_path) -> None:
    store = SQLiteRepository(tmp_path / "expiry.db")
    provider = ControlledProvider()
    service = GatewayService(store, provider, PlatformConfig(pool_capacity=1))
    await service.start()
    if "alice" not in service.accounts.accounts:
        await service.accounts.register(
            AccountLogin(username="alice", password="test-password-123")
        )
    try:
        await service.register_root("alice", RootRegistration(root_task_id="root"))
        await service.submit("alice", request("running"))
        await provider.started.wait()
        expiring = request("expiring").model_copy(
            update={"deadline_at": (datetime.now(UTC) + timedelta(seconds=0.1)).isoformat()}
        )
        await service.submit("alice", expiring)
        await until(lambda: service.get("alice", "expiring").state == "FINISHED")
        assert service.get("alice", "expiring").error == "DEADLINE_EXPIRED"
        assert provider.calls == 1
    finally:
        await service.close()
        await store.close()


async def test_accounts_share_one_pool_with_separate_inflight_and_balances(tmp_path) -> None:
    store = SQLiteRepository(tmp_path / "platform.db")
    provider = ControlledProvider()
    service = GatewayService(
        store, provider, PlatformConfig(pool_capacity=2, account_max_inflight=1)
    )
    await service.start()
    try:
        for username in ("alice", "bob"):
            await service.accounts.register(
                AccountLogin(username=username, password="test-password-123")
            )
        for username, root in (("alice", "a1"), ("alice", "a2"), ("bob", "b1")):
            await service.register_root(username, RootRegistration(root_task_id=root))
        await service.submit("alice", request("same-id", "a1"))
        await provider.started.wait()
        await service.submit("alice", request("second", "a2"))
        await service.submit("bob", request("same-id", "b1"))
        await until(lambda: provider.calls == 2)
        assert provider.roots == ["a1", "b1"]
        assert len(set(provider.identities)) == 2
        assert service.quota.inflight == 2
        assert (await service.feedback("alice")).account_inflight == 1
        assert (await service.feedback("bob")).account_inflight == 1
        provider.release.set()
        await until(lambda: all(r.state == "FINISHED" for r in service.requests.values()))
        alice = await service.feedback("alice")
        bob = await service.feedback("bob")
        assert alice.account_consumed_units == 4 and bob.account_consumed_units == 2
        assert alice.account_reserved_units == bob.account_reserved_units == 0
        assert service.quota.consumed_units == 6 and service.quota.inflight == 0
    finally:
        await service.close()
        await store.close()


async def test_account_budget_exhaustion_does_not_consume_shared_capacity(tmp_path) -> None:
    class CostlyProvider(ControlledProvider):
        @staticmethod
        def reply() -> ModelReply:
            return ModelReply(
                content="done", usage=Usage(input_tokens=200, output_tokens=200, cost_units=400)
            )

    store = SQLiteRepository(tmp_path / "platform.db")
    provider = CostlyProvider()
    service = GatewayService(
        store, provider, PlatformConfig(pool_capacity=1, account_budget_units=600)
    )
    await service.start()
    try:
        await service.accounts.register(
            AccountLogin(username="alice", password="test-password-123")
        )
        await service.register_root("alice", RootRegistration(root_task_id="root"))
        await service.submit("alice", request("first"))
        await provider.started.wait()
        await service.submit("alice", request("second"))
        provider.release.set()
        await until(lambda: service.get("alice", "second").state == "FINISHED")
        assert service.get("alice", "second").outcome == Outcome.FAILED
        assert provider.calls == 1
        assert service.quota.consumed_units == 400 and service.quota.inflight == 0
        assert (await service.feedback("alice")).account_remaining_units == 200
    finally:
        await service.close()
        await store.close()


async def test_legacy_unknown_request_keeps_its_original_pool_receipt(tmp_path) -> None:
    from taskloom.contracts.models import GatewayRecord, QuotaState, digest
    from taskloom.domain.ports import StoredRecord

    class ReceiptProvider(ControlledProvider):
        async def reconcile(self, request_id):
            assert request_id == "legacy-request"
            return self.reply()

    store = SQLiteRepository(tmp_path / "legacy.db")
    old = request("legacy-request")
    record = GatewayRecord(
        tenant_id="alice",
        request=old,
        payload_hash=digest(old.model_dump()),
        state="UNKNOWN",
        outcome=Outcome.UNKNOWN,
        reserved_units=old.reserve_units,
    )
    await store.apply(
        [
            StoredRecord(
                "request", "alice:legacy-request", "alice", record.model_dump(mode="json")
            ),
            StoredRecord(
                "quota",
                "shared",
                "_platform",
                QuotaState(reserved_units=old.reserve_units, inflight=1).model_dump(),
            ),
        ]
    )
    service = GatewayService(store, ReceiptProvider(), PlatformConfig())
    await service.start()
    try:
        assert service.get("alice", "legacy-request").state == "FINISHED"
        assert service.quota.inflight == service.quota.reserved_units == 0
        assert service.quota.consumed_units == 2
        assert service.account_quotas["alice"].consumed_units == 2
    finally:
        await service.close()
        await store.close()
