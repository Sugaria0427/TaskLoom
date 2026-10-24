import asyncio
import json
import math
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from taskloom.adapters.provider import HTTPProvider
from taskloom.adapters.sqlite import SQLiteRepository
from taskloom.contracts.models import (
    AccountLogin,
    ModelReply,
    ModelRequest,
    PlatformConfig,
    RootRegistration,
    Usage,
    mock_tokens,
)
from taskloom.domain.model import DomainError, Outcome
from taskloom.gateway.service import GatewayService
from taskloom.policies.reservation import Prices


class ControlledProvider:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.fail = False
        self.receipt = False
        self.roots = []
        self.identities = []

    async def generate(self, request: ModelRequest, account: str | None = None) -> ModelReply:
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

    async def reconcile(self, request_id: str, account: str | None = None) -> ModelReply | None:
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
        async def reconcile(self, request_id, account=None):
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


async def test_http_provider_maps_only_unambiguous_refusals() -> None:
    import httpx

    from taskloom.adapters.provider import HTTPProvider
    from taskloom.gateway.ports import ProviderRejected

    outcomes: list[Exception | int] = []

    def answer(_request: httpx.Request) -> httpx.Response:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, json={})

    provider = HTTPProvider("http://pool", "taskloom-fixed-model")
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(
        base_url="http://pool", transport=httpx.MockTransport(answer)
    )
    try:
        for refusal in (429, httpx.ConnectError("refused")):
            outcomes.append(refusal)
            with pytest.raises(ProviderRejected):
                await provider.generate(request("refused"))
        # Failures after the request may have reached the pool stay ambiguous; a proxy can
        # answer 503 after the upstream ran.
        for ambiguous in (500, 502, 503, httpx.ReadTimeout("slow")):
            outcomes.append(ambiguous)
            with pytest.raises(httpx.HTTPError) as error:
                await provider.generate(request("ambiguous"))
            assert not isinstance(error.value, ProviderRejected)
    finally:
        await provider.close()


async def test_pool_refusal_requeues_and_releases_the_slot(tmp_path) -> None:
    from taskloom.gateway.ports import ProviderRejected

    class RefusingProvider(ControlledProvider):
        async def generate(self, request: ModelRequest, account: str | None = None) -> ModelReply:
            self.calls += 1
            if self.calls == 1:
                raise ProviderRejected("pool busy")
            return self.reply()

    store = SQLiteRepository(tmp_path / "gateway.db")
    provider = RefusingProvider()
    service = GatewayService(store, provider, PlatformConfig(pool_capacity=1))
    service.reject_backoff_seconds = 0.05
    await service.start()
    await service.accounts.register(AccountLogin(username="alice", password="test-password-123"))
    try:
        await service.register_root("alice", RootRegistration(root_task_id="root"))
        await service.submit("alice", request("refused-once"))
        await until(lambda: service.get("alice", "refused-once").state == "FINISHED")
        record = service.get("alice", "refused-once")
        assert (record.outcome, record.dispatch_attempts, provider.calls) == (
            Outcome.SUCCEEDED,
            1,
            2,
        )
        assert service.quota.inflight == service.quota.reserved_units == 0
        assert (await store.get("root_usage", "alice:root"))["consumed_units"] == 2
    finally:
        await service.close()
        await store.close()


def pool_transport(
    output_tokens: int, cached_tokens: int = 0, first_status: int | None = None
) -> tuple[httpx.MockTransport, dict[str, int]]:
    """An OpenAI-compatible pool that runs every POST once and answers queries by request id.

    `first_status` makes the first completion fail with that status after it has run.
    """
    replies: dict[str, dict[str, object]] = {}
    counts = {"posts": 0, "queries_allowed": 0}

    def answer(http: httpx.Request) -> httpx.Response:
        if http.method == "GET":
            request_id = http.url.path.rsplit("/", 1)[-1]
            if not counts["queries_allowed"] or request_id not in replies:
                return httpx.Response(404, json={})
            return httpx.Response(200, json={"response": replies[request_id]})
        counts["posts"] += 1
        body = json.loads(http.content)
        reply = {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {
                "prompt_tokens": mock_tokens(
                    {"messages": body["messages"], "tools": body["tools"]}
                ),
                "completion_tokens": min(output_tokens, body["max_tokens"]),
                "prompt_tokens_details": {"cached_tokens": cached_tokens},
            },
        }
        replies[http.headers["X-TaskLoom-Request-Id"]] = reply
        if first_status is not None and counts["posts"] == 1:
            return httpx.Response(first_status, json={})
        return httpx.Response(200, json=reply)

    return httpx.MockTransport(answer), counts


async def http_provider(transport: httpx.MockTransport, config: PlatformConfig) -> HTTPProvider:
    provider = HTTPProvider(
        "http://pool",
        config.model,
        prices=Prices(config.output_price, config.cache_read_price, config.cache_write_price),
    )
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(base_url="http://pool", transport=transport)
    return provider


async def started(
    store: SQLiteRepository, provider: object, config: PlatformConfig
) -> GatewayService:
    service = GatewayService(store, provider, config)  # type: ignore[arg-type]
    await service.start()
    await service.accounts.register(AccountLogin(username="alice", password="test-password-123"))
    await service.register_root("alice", RootRegistration(root_task_id="root"))
    return service


async def test_http_charges_use_platform_prices(tmp_path) -> None:
    # Output at 0.1 and cache reads at 0.1 of an uncached input token: 2 budgets of 70 admit
    # three worst cases of about 21; billing raw tokens instead would charge about 200.
    config = PlatformConfig(
        output_price=0.1, cache_read_price=0.1, total_budget_units=70, account_budget_units=70
    )
    transport, counts = pool_transport(output_tokens=50, cached_tokens=10)
    store = SQLiteRepository(tmp_path / "gateway.db")
    service = await started(store, await http_provider(transport, config), config)
    try:
        names = ["a", "b", "c"]
        for name in names:
            await service.submit("alice", request(name).model_copy(update={"max_tokens": 50}))
        await until(lambda: all(service.get("alice", n).state == "FINISHED" for n in names))
        prompt = request("a").input_tokens
        charge = math.ceil(prompt - 10 + 10 * 0.1 + 50 * 0.1 - 1e-9)
        assert [service.get("alice", n).consumed_units for n in names] == [charge] * 3
        assert service.quota.consumed_units == 3 * charge <= config.total_budget_units
        assert counts["posts"] == 3
    finally:
        await service.close()
        await store.close()


async def test_token_window_admits_worst_case_not_estimate(tmp_path) -> None:
    # A short reply teaches the estimator to expect about one output token; the next two
    # requests then use all 50. Admitting on the estimate let both in (134/100).
    config = PlatformConfig(
        pool_capacity=2, account_max_inflight=2, tokens_per_window=100,
        account_tokens_per_window=100, window_seconds=600,
    )  # fmt: skip
    outputs = iter([1, 50, 50])

    class Provider(ControlledProvider):
        async def generate(self, request: ModelRequest, account: str | None = None) -> ModelReply:
            self.calls += 1
            output = next(outputs)
            used = request.input_tokens + output
            return ModelReply(
                content="ok",
                usage=Usage(
                    input_tokens=request.input_tokens, output_tokens=output, cost_units=used
                ),
            )

    store = SQLiteRepository(tmp_path / "gateway.db")
    provider = Provider()
    service = await started(store, provider, config)
    try:
        await service.submit("alice", request("warm").model_copy(update={"max_tokens": 50}))
        await until(lambda: service.get("alice", "warm").state == "FINISHED")
        for name in ("one", "two"):
            await service.submit("alice", request(name).model_copy(update={"max_tokens": 50}))
        await until(lambda: service.get("alice", "one").state == "FINISHED")
        await asyncio.sleep(0.3)
        # The second worst case does not fit the window, so it waits for the next one.
        assert service.get("alice", "two").state == "QUEUED"
        assert provider.calls == 2
        assert service.quota.window_tokens <= config.tokens_per_window
    finally:
        await service.close()
        await store.close()


async def test_503_after_execution_is_reconciled_not_replayed(tmp_path) -> None:
    config = PlatformConfig()
    transport, counts = pool_transport(output_tokens=5, first_status=503)
    store = SQLiteRepository(tmp_path / "gateway.db")
    service = await started(store, await http_provider(transport, config), config)
    try:
        await service.submit("alice", request("ran"))
        await until(lambda: service.get("alice", "ran").state == "UNKNOWN")
        # Until the pool confirms, the request keeps its reservation and slot.
        unknown = service.get("alice", "ran")
        assert unknown.reserved_units > 0 and service.quota.inflight == 1
        counts["queries_allowed"] = 1
        await until(lambda: service.get("alice", "ran").state == "FINISHED")
        settled = service.get("alice", "ran")
        assert settled.outcome == Outcome.SUCCEEDED and counts["posts"] == 1
        assert service.quota.consumed_units == settled.consumed_units > 0
        assert service.quota.inflight == service.quota.reserved_units == 0
    finally:
        await service.close()
        await store.close()


async def test_cancelled_queue_entries_leave_the_policy_while_the_pool_is_full(tmp_path) -> None:
    config = PlatformConfig(pool_capacity=1, max_queue=1)
    store = SQLiteRepository(tmp_path / "gateway.db")
    provider = ControlledProvider()
    service = await started(store, provider, config)
    try:
        await service.submit("alice", request("busy"))
        await provider.started.wait()
        for index in range(20):
            await service.submit("alice", request(f"q{index}"))
            await service.cancel("alice", f"q{index}")
        assert len(service.policy) == 0
        provider.release.set()
        await until(lambda: service.get("alice", "busy").state == "FINISHED")
        assert provider.calls == 1
    finally:
        await service.close()
        await store.close()
