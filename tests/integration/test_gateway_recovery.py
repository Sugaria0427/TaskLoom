import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest

from taskloom.adapters.sqlite import SQLiteRepository
from taskloom.contracts.models import (
    AccountLogin,
    GatewayRecord,
    ModelReply,
    ModelRequest,
    PlatformConfig,
    QuotaState,
    RootRegistration,
    Usage,
    digest,
)
from taskloom.domain.model import Outcome
from taskloom.domain.ports import StoredRecord
from taskloom.gateway.service import GatewayService


def request(name: str) -> ModelRequest:
    result = ModelRequest(
        request_id=name,
        root_task_id="root",
        agent_id="agent",
        step_id=name,
        attempt_id=name,
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=1,
        deadline_at=(datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
    )
    result.max_tokens = 70 - result.input_tokens
    return result


class RecoveryProvider:
    def __init__(self) -> None:
        self.confirmed = False
        self.generated = 0

    async def generate(self, req: ModelRequest, account: str | None = None) -> ModelReply:
        self.generated += 1
        return self.reply(req)

    @staticmethod
    def reply(req: ModelRequest) -> ModelReply:
        return ModelReply(
            content="done",
            usage=Usage(
                input_tokens=req.input_tokens,
                output_tokens=req.max_tokens,
                cost_units=70,
            ),
        )

    async def reconcile(self, request_id: str, account: str | None = None) -> ModelReply | None:
        assert request_id == "legacy"
        return self.reply(request("legacy")) if self.confirmed else None

    async def close(self) -> None:
        pass


@pytest.mark.parametrize("state", ["DISPATCH_INTENT", "UNKNOWN"])
async def test_legacy_hard_holds_survive_repeated_recovery(tmp_path, state: str) -> None:
    path = tmp_path / "platform.db"
    config = PlatformConfig(
        pool_capacity=2,
        account_max_inflight=2,
        total_budget_units=100,
        account_budget_units=100,
    )
    provider = RecoveryProvider()
    store = SQLiteRepository(path)
    setup = GatewayService(store, provider, config)
    await setup.start()
    await setup.accounts.register(AccountLogin(username="alice", password="password-12345"))
    await setup.register_root("alice", RootRegistration(root_task_id="root", budget_units=100))
    await setup.close()

    old = request("legacy")
    row = GatewayRecord(
        tenant_id="alice",
        request=old,
        payload_hash=digest(old.model_dump(mode="json")),
        state=state,
        outcome=Outcome.PENDING if state == "DISPATCH_INTENT" else Outcome.UNKNOWN,
        reserved_units=70,
        quiescent=False,
        cancel_requested=True,
        dispatched_at=datetime.now(UTC).isoformat(),
    ).model_dump(mode="json")
    for field in (
        "worst_units",
        "window_tokens",
        "upstream_account",
        "dispatch_attempts",
        "not_before",
    ):
        row.pop(field)
    quota = QuotaState(
        reserved_units=70,
        inflight=1,
        window_start=time.time(),
        window_requests=1,
        window_tokens=70,
    ).model_dump()
    quota.pop("worst_units")
    await store.apply(
        [
            StoredRecord("request", "alice:legacy", "alice", row),
            StoredRecord("quota", "shared", "_platform", quota),
            StoredRecord(
                "account_quota", "alice", "alice", {"account_id": "alice", "quota": quota}
            ),
        ]
    )
    await store.close()

    for restart in range(2):
        recovered_store = SQLiteRepository(path)
        recovered = GatewayService(recovered_store, provider, config)
        await recovered.start()
        try:
            restored = recovered.get("alice", "legacy")
            assert restored.worst_units == restored.reserved_units == restored.window_tokens == 70
            assert restored.cancel_requested and restored.request.request_id == "legacy"
            for ledger in (recovered.quota, recovered.account_quotas["alice"]):
                assert ledger.worst_units == ledger.reserved_units == 70
                assert ledger.inflight == 1
            if restart == 0:
                await recovered.submit("alice", request("new"))
            await asyncio.sleep(0.05)
            assert recovered.get("alice", "new").state == "QUEUED"
            assert provider.generated == 0
            if restart == 1:
                provider.confirmed = True
                await recovered.reconcile()
                await recovered.reconcile()
                async with asyncio.timeout(2):
                    while recovered.get("alice", "new").state != "FINISHED":  # noqa: ASYNC110
                        await asyncio.sleep(0.005)
                assert recovered.get("alice", "legacy").outcome == Outcome.CANCELLED
                assert recovered.get("alice", "new").error == "BUDGET_EXHAUSTED"
                assert provider.generated == 0
                for ledger in (recovered.quota, recovered.account_quotas["alice"]):
                    assert ledger.consumed_units == 70
                    assert ledger.worst_units == ledger.reserved_units == ledger.inflight == 0
                assert recovered.quota.window_tokens == 70
        finally:
            await recovered.close()
            await recovered_store.close()
