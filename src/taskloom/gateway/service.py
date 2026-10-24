import asyncio
import math
import time
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal

from taskloom.contracts.models import (
    GatewayRecord,
    JsonObject,
    ModelReply,
    ModelRequest,
    PlatformConfig,
    PlatformFeedback,
    QuotaState,
    RootRegistration,
    digest,
    identity,
)
from taskloom.domain.model import DomainError, Outcome
from taskloom.domain.ports import Repository, StoredRecord
from taskloom.gateway.accounts import AccountService
from taskloom.gateway.ports import ModelProvider, ProviderRejected
from taskloom.policies.placement import AffinityPlacement
from taskloom.policies.reservation import Estimate, Prices, ReservationEstimator, worst_units
from taskloom.policies.scheduling import scheduling_policy

LIVE_STATES = ("QUEUED", "DISPATCH_INTENT", "UNKNOWN")


class GatewayService:
    # Admission bounds live work only; finished records leave memory after a retention and
    # are then served from the store, which keeps every row.
    max_live_requests = 1024
    finished_retention_seconds = 300.0
    max_finished_records = 1024
    # Root registrations per window: a rate guard on registry growth, not a lifetime cap.
    max_recent_roots = 1024
    root_window_seconds = 600.0
    # Pre-execution pool refusals back off exponentially and fail after a bounded count.
    reject_backoff_seconds = 1.0
    reject_backoff_cap_seconds = 30.0
    max_pool_rejections = 6

    def __init__(
        self,
        store: Repository,
        provider: ModelProvider,
        config: PlatformConfig,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        # Deadlines, windows and record timestamps share one clock so simulations stay consistent.
        self.clock = clock
        self.provider = provider
        self.config = config
        self.requests: dict[str, GatewayRecord] = {}
        self.quota = QuotaState()
        self.accounts = AccountService(store, config)
        self.account_quotas: dict[str, QuotaState] = {}
        self.policy = scheduling_policy(config.scheduling_policy)
        # Without configured pool accounts the pool is one account and nothing is placed.
        self.placement = (
            AffinityPlacement(
                {a.account_id: a.capacity for a in config.pool_accounts},
                config.cache_wait_seconds,
            )
            if config.pool_accounts
            else None
        )
        self.lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self._loop: asyncio.Task[None] | None = None
        self._active: dict[str, asyncio.Task[None]] = {}
        self._recent_roots: dict[str, float] = {}
        self._next_prune = 0.0
        self.ready = False
        self.prices = Prices(config.output_price, config.cache_read_price, config.cache_write_price)
        self.estimator = ReservationEstimator(self.prices)

    def now(self) -> str:
        return datetime.fromtimestamp(self.clock(), UTC).isoformat()

    @staticmethod
    def key(tenant: str, request_id: str) -> str:
        return tenant + ":" + request_id

    async def start(self) -> None:
        await self.accounts.start()
        quota = await self.store.get("quota", "shared")
        self.quota = QuotaState.model_validate(quota) if quota else QuotaState()
        for item in await self.store.records("account_quota"):
            self.account_quotas[item["account_id"]] = QuotaState.model_validate(item["quota"])
        usage = {row["root_key"] for row in await self.store.records("root_usage")}
        legacy: dict[str, tuple[str, int]] = {}
        for row in await self.store.records("request"):
            record = GatewayRecord.model_validate(row)
            key = self.key(record.tenant_id, record.request.request_id)
            self.requests[key] = record
            root_key = self.key(record.tenant_id, record.request.root_task_id)
            if record.consumed_units and root_key not in usage:
                _, units = legacy.get(root_key, (record.tenant_id, 0))
                legacy[root_key] = (record.tenant_id, units + record.consumed_units)
            if record.state == "QUEUED":
                self._enqueue(record.tenant_id, record.request.root_task_id, key)
                continue
            if record.outcome == Outcome.PENDING:
                record.state = "UNKNOWN"
                record.outcome = Outcome.UNKNOWN
                record.error = "Dispatch interrupted; reconciliation required"
                await self._save(record)
            if record.state == "UNKNOWN":
                # Unresolved work still holds its slot; the policy counts it until settled.
                self.policy.on_dispatch(
                    key, record.tenant_id, record.request.root_task_id, self.clock(),
                    record.reserved_units,
                )  # fmt: skip
        # State written before per-root usage was persisted derives it once from full history.
        for root_key, (tenant, units) in legacy.items():
            await self.store.apply([self._root_usage(root_key, tenant, units)])
        for tenant in {r.tenant_id for r in self.requests.values()}:
            self._account_quota(tenant)
        self._prune()
        await self.reconcile()
        self.ready = True
        self._loop = asyncio.create_task(self._schedule(), name="gateway-dispatch")

    def _enqueue(self, tenant: str, root: str, key: str) -> None:
        self.policy.push(tenant, root, key, self.clock())
        self.wake.set()

    def _candidate(self) -> str | None:
        # The policy orders tenants, then registered root tasks; the gateway judges feasibility.
        busy = self._upstream_busy()
        if self.placement is not None and not self.placement.has_free(busy):
            return None
        now = self.clock()
        # A request waiting for its cache's account steps aside only for tenants holding no
        # more slots than its own; otherwise a heavier tenant would take the free slot, often
        # for minutes, and caching would undo max-min fairness.
        ceiling: int | None = None

        def feasible(item: str) -> bool:
            nonlocal ceiling
            # Keys leave the queue when their request finishes; this guards stale ones.
            if item not in self.requests:
                return True
            record = self.requests[item]
            held = self._account_quota(record.tenant_id).inflight
            if ceiling is not None and held > ceiling:
                return False
            if not self._feasible(record):
                return False
            if self.placement is None or self.placement.ready(self._chain(record), busy, now):
                return True
            ceiling = held if ceiling is None else min(ceiling, held)
            return False

        return self.policy.pick(feasible, now)

    def _chain(self, record: GatewayRecord) -> str:
        # A chain is one agent of one user: the unit that carries a prompt cache.
        return self.key(record.tenant_id, record.request.agent_id)

    def _upstream_busy(self) -> Counter[str]:
        # Unknown outcomes keep their upstream slot, like their reservation, until reconciled.
        return Counter(
            r.upstream_account
            for r in self.requests.values()
            if r.upstream_account is not None and r.state in ("DISPATCH_INTENT", "UNKNOWN")
        )

    def _place(self, record: GatewayRecord) -> str | None:
        if self.placement is None:
            return None
        # _candidate guaranteed a free account.
        return self.placement.choose(self._chain(record), self._upstream_busy(), self.clock())

    async def register_root(self, tenant: str, root: RootRegistration) -> None:
        self.accounts.get(tenant)
        async with self.lock:
            key = self.key(tenant, root.root_task_id)
            previous = await self.store.get("root", key)
            data = root.model_dump()
            data["tenant_id"] = tenant
            if previous is not None and previous != data:
                raise DomainError("ROOT_CONFLICT", "Root task metadata changed")
            if previous is None:
                horizon = self.clock() - self.root_window_seconds
                self._recent_roots = {k: at for k, at in self._recent_roots.items() if at > horizon}
                if len(self._recent_roots) >= self.max_recent_roots:
                    raise DomainError("QUEUE_FULL", "Root registry capacity exhausted", True)
            await self.store.apply([StoredRecord("root", key, tenant, data)])
            if previous is None:
                self._recent_roots[key] = self.clock()

    async def submit(self, tenant: str, request: ModelRequest) -> GatewayRecord:
        async with self.lock:
            account = self.accounts.get(tenant)
            key = self.key(tenant, request.request_id)
            payload_hash = digest(request.model_dump(mode="json"))
            previous = self.requests.get(key)
            if previous is None:
                # Duplicates of pruned (finished) records are answered from the store.
                stored = await self.store.get("request", key)
                previous = GatewayRecord.model_validate(stored) if stored else None
            if previous is not None:
                if previous.payload_hash != payload_hash:
                    raise DomainError(
                        "DUPLICATE_ID_CONFLICT", "Request identity has different content"
                    )
                return previous.model_copy(deep=True)
            root = await self.store.get("root", self.key(tenant, request.root_task_id))
            if root is None:
                raise DomainError("ROOT_NOT_REGISTERED", "Register this user's root task first")
            # Budgets admit worst cases; windows must hold the request even if it produces
            # max_tokens, so a queued request is never infeasible forever.
            worst = self.worst(request)
            if worst > min(self.config.total_budget_units, root["budget_units"]):
                raise DomainError("REQUEST_TOO_LARGE", "Request exceeds the budget domain")
            if request.reserve_units > self.config.tokens_per_window:
                raise DomainError("REQUEST_TOO_LARGE", "Request exceeds the token window capacity")
            if worst > account.budget_units or request.reserve_units > account.tokens_per_window:
                raise DomainError("REQUEST_TOO_LARGE", "Request exceeds the account allowance")
            if datetime.fromisoformat(request.deadline_at).timestamp() <= self.clock():
                raise DomainError("DEADLINE_EXPIRED", "Request deadline already expired")
            queued = [r for r in self.requests.values() if r.state == "QUEUED"]
            live = [r for r in self.requests.values() if r.state in LIVE_STATES]
            size = sum(len(r.request.model_dump_json().encode()) for r in queued)
            live_size = sum(len(r.request.model_dump_json().encode()) for r in live)
            if (
                len(queued) >= self.config.max_queue
                or sum(r.tenant_id == tenant for r in queued) >= self.config.max_tenant_queue
                or size + len(request.model_dump_json().encode()) > self.config.max_queue_bytes
                or len(live) >= self.max_live_requests
                or live_size + len(request.model_dump_json().encode())
                > self.config.max_history_bytes
            ):
                raise DomainError("QUEUE_FULL", "Admission capacity exhausted", True)
            record = GatewayRecord(
                tenant_id=tenant, request=request, payload_hash=payload_hash, created_at=self.now()
            )
            await self._save(record)
            self.requests[key] = record
            self._enqueue(tenant, request.root_task_id, key)
            return record.model_copy(deep=True)

    def get(self, tenant: str, request_id: str) -> GatewayRecord:
        """Live and recently finished records; `query` also finds pruned ones."""
        record = self.requests.get(self.key(tenant, request_id))
        if record is None:
            raise DomainError("NOT_FOUND", "Request not found")
        return record.model_copy(deep=True)

    async def query(self, tenant: str, request_id: str) -> GatewayRecord:
        key = self.key(tenant, request_id)
        if key in self.requests:
            return self.get(tenant, request_id)
        stored = await self.store.get("request", key)
        if stored is None:
            raise DomainError("NOT_FOUND", "Request not found")
        return GatewayRecord.model_validate(stored)

    async def cancel(self, tenant: str, request_id: str) -> GatewayRecord:
        async with self.lock:
            if self.key(tenant, request_id) not in self.requests:
                # Only finished records are pruned; cancelling them changes nothing.
                return await self.query(tenant, request_id)
            record = self.get(tenant, request_id)
            record.cancel_requested = True
            if record.state == "QUEUED":
                record.state = "FINISHED"
                record.outcome = Outcome.CANCELLED
                record.quiescent = True
                record.completed_at = self.now()
                self.policy.discard(self.key(tenant, request_id))
            await self._save(record)
            self.requests[self.key(tenant, request_id)] = record
            self.wake.set()
            return record.model_copy(deep=True)

    def _refresh_window(self) -> None:
        for quota in [self.quota, *self.account_quotas.values()]:
            if self.clock() >= quota.window_start + self.config.window_seconds:
                quota.window_start = self.clock()
                quota.window_requests = 0
                quota.window_tokens = 0

    def _account_quota(self, tenant: str) -> QuotaState:
        if tenant not in self.account_quotas:
            # Derived from memory only for state without a persisted account quota; start()
            # derives those before pruning, and every later dispatch persists one.
            records = [r for r in self.requests.values() if r.tenant_id == tenant]
            self.account_quotas[tenant] = QuotaState(
                window_start=self.quota.window_start or self.clock(),
                consumed_units=sum(r.consumed_units for r in records),
                reserved_units=sum(r.reserved_units for r in records),
                worst_units=sum(r.worst_units for r in records),
                inflight=sum(bool(r.reserved_units) for r in records),
            )
        return self.account_quotas[tenant]

    def worst(self, request: ModelRequest) -> int:
        return worst_units(request.input_tokens, request.max_tokens, self.prices)

    def _estimate(self, record: GatewayRecord) -> Estimate:
        request = record.request
        return self.estimator.estimate(
            record.tenant_id, request.agent_id, request.input_tokens, request.max_tokens,
            self.clock(),
        )  # fmt: skip

    @staticmethod
    def _in_window(record: GatewayRecord, quota: QuotaState) -> bool:
        # Dispatch times round-trip through ISO text, exact to the microsecond.
        dispatched = record.dispatched_at
        return (
            dispatched is not None
            and datetime.fromisoformat(dispatched).timestamp() >= quota.window_start - 1e-6
        )

    def _feasible(self, record: GatewayRecord) -> bool:
        if record.state != "QUEUED":
            return True
        # Backoff after a pool refusal.
        not_before = record.not_before
        if not_before and datetime.fromisoformat(not_before).timestamp() > self.clock():
            return False
        account = self.accounts.accounts.get(record.tenant_id)
        if account is None:
            return False
        personal = self._account_quota(record.tenant_id)
        # Budgets and token windows are hard limits, so both admit the worst case: the full
        # prompt plus max_tokens. The estimate only orders and reports; settlement returns
        # what a request did not use.
        worst = self.worst(record.request)
        tokens = record.request.reserve_units
        return (
            self.quota.inflight < self.config.pool_capacity
            and self.quota.window_requests < self.config.requests_per_window
            and self.quota.window_tokens + tokens <= self.config.tokens_per_window
            and self.quota.consumed_units + self.quota.worst_units + worst
            <= self.config.total_budget_units
            and personal.inflight < account.max_inflight
            and personal.window_requests < account.requests_per_window
            and personal.window_tokens + tokens <= account.tokens_per_window
            and personal.consumed_units + personal.worst_units + worst <= account.budget_units
        )

    async def _save(
        self,
        record: GatewayRecord,
        quota: QuotaState | None = None,
        account_quota: QuotaState | None = None,
        extra: Sequence[StoredRecord] = (),
    ) -> None:
        records = [
            StoredRecord(
                "request",
                self.key(record.tenant_id, record.request.request_id),
                record.tenant_id,
                record.model_dump(mode="json"),
            )
        ]
        if quota is not None:
            records.append(StoredRecord("quota", "shared", "_platform", quota.model_dump()))
        if account_quota is not None:
            records.append(
                StoredRecord(
                    "account_quota",
                    record.tenant_id,
                    record.tenant_id,
                    {"account_id": record.tenant_id, "quota": account_quota.model_dump()},
                )
            )
        records.extend(extra)
        await self.store.apply(
            records,
            {
                "schema_version": 1,
                "event_id": identity(),
                "tenant_id": record.tenant_id,
                "root_task_id": record.request.root_task_id,
                "attempt_id": record.request.attempt_id,
                "type": "request." + record.state.lower(),
                "occurred_at": self.now(),
            },
        )

    async def _schedule(self) -> None:
        while True:
            async with self.lock:
                self._refresh_window()
                # Expiration must also advance requests whose quota is currently infeasible.
                for expired_key, old in list(self.requests.items()):
                    if old.state != "QUEUED":
                        continue
                    expired = (
                        datetime.fromisoformat(old.request.deadline_at).timestamp() <= self.clock()
                    )
                    # Consumption only grows, so a worst case beyond it never fits again.
                    worst = self.worst(old.request)
                    exhausted = self.quota.consumed_units + worst > self.config.total_budget_units
                    account = self.accounts.accounts.get(old.tenant_id)
                    if account is not None:
                        exhausted = exhausted or (
                            self._account_quota(old.tenant_id).consumed_units + worst
                            > account.budget_units
                        )
                    if expired or exhausted:
                        rejected = old.model_copy(deep=True)
                        rejected.state = "FINISHED"
                        rejected.outcome = Outcome.FAILED
                        rejected.quiescent = True
                        rejected.error = "DEADLINE_EXPIRED" if expired else "BUDGET_EXHAUSTED"
                        await self._save(rejected)
                        self.requests[expired_key] = rejected
                        self.policy.discard(expired_key)
                self._prune()
                while self.quota.inflight < self.config.pool_capacity:
                    key = self._candidate()
                    if key is None:
                        break
                    if key not in self.requests or self.requests[key].state != "QUEUED":
                        continue
                    record = self.requests[key].model_copy(deep=True)
                    if (
                        datetime.fromisoformat(record.request.deadline_at).timestamp()
                        < self.clock()
                    ):
                        record.outcome = Outcome.FAILED
                        record.state = "FINISHED"
                        record.error = "DEADLINE_EXPIRED"
                        record.quiescent = True
                        await self._save(record)
                        self.requests[key] = record
                        continue
                    root_key = self.key(record.tenant_id, record.request.root_task_id)
                    root = await self.store.get("root", root_key)
                    # Consumption is persisted per root, since finished records get pruned.
                    root_used = await self._root_consumed(root_key) + sum(
                        r.worst_units
                        for r in self.requests.values()
                        if r.tenant_id == record.tenant_id
                        and r.request.root_task_id == record.request.root_task_id
                    )
                    worst = self.worst(record.request)
                    if root is None or root_used + worst > root["budget_units"]:
                        record.state = "FINISHED"
                        record.outcome = Outcome.FAILED
                        record.error = "ROOT_BUDGET_EXHAUSTED"
                        record.quiescent = True
                        await self._save(record)
                        self.requests[key] = record
                        continue
                    quota = self.quota.model_copy(deep=True)
                    account_quota = self._account_quota(record.tenant_id).model_copy(deep=True)
                    record.state = "DISPATCH_INTENT"
                    record.pool_request_id = digest([record.tenant_id, record.request.request_id])
                    record.dispatched_at = self.now()
                    record.upstream_account = self._place(record)
                    estimate = self._estimate(record)
                    record.reserved_units = estimate.units
                    record.worst_units = worst
                    record.window_tokens = record.request.reserve_units
                    for ledger in (quota, account_quota):
                        ledger.reserved_units += record.reserved_units
                        ledger.worst_units += record.worst_units
                        ledger.inflight += 1
                        ledger.window_requests += 1
                        ledger.window_tokens += record.window_tokens
                    self.estimator.dispatched(
                        key, record.tenant_id, record.request.agent_id,
                        record.request.input_tokens, self.clock(),
                    )  # fmt: skip
                    await self._save(record, quota, account_quota)
                    self.requests[key] = record
                    self.quota = quota
                    self.account_quotas[record.tenant_id] = account_quota
                    self._active[key] = asyncio.create_task(self._dispatch(key))
                    self.policy.on_dispatch(
                        key, record.tenant_id, record.request.root_task_id, self.clock(),
                        record.reserved_units,
                    )  # fmt: skip
            try:
                await asyncio.wait_for(self.wake.wait(), 0.1)
            except TimeoutError:
                pass
            self.wake.clear()
            await self.reconcile()

    async def _dispatch(self, key: str) -> None:
        record = self.requests[key]
        try:
            # Account-scoped request identities remain distinct in the shared model pool.
            upstream = record.request.model_copy(
                update={"request_id": self.pool_request_id(record)}
            )
            reply = await self.provider.generate(upstream, account=record.upstream_account)
            await self._finish(key, reply)
        except ProviderRejected as error:
            await self._rejected(key, error)
        except (Exception, asyncio.CancelledError) as error:
            async with self.lock:
                current = self.requests[key].model_copy(deep=True)
                current.state = "UNKNOWN"
                current.outcome = Outcome.UNKNOWN
                current.error = type(error).__name__ + ": awaiting provider reconciliation"
                await self._save(current)
                self.requests[key] = current
        finally:
            self._active.pop(key, None)
            self.wake.set()

    async def _rejected(self, key: str, error: ProviderRejected) -> None:
        async with self.lock:
            current = self.requests[key]
            if current.state != "DISPATCH_INTENT":
                return
            record = current.model_copy(deep=True)
            quota = self.quota.model_copy(deep=True)
            account_quota = self._account_quota(record.tenant_id).model_copy(deep=True)
            # Release the reservation and slot. Window counters keep the attempt: windows
            # meter dispatches offered to the pool, and keeping them also slows retries
            # against a pool that keeps refusing.
            for ledger in (quota, account_quota):
                ledger.reserved_units -= record.reserved_units
                ledger.worst_units -= record.worst_units
                ledger.inflight -= 1
            record.reserved_units = 0
            record.worst_units = 0
            record.dispatch_attempts += 1
            deadline = datetime.fromisoformat(record.request.deadline_at).timestamp()
            if record.cancel_requested:
                record.state = "FINISHED"
                record.outcome = Outcome.CANCELLED
            elif record.dispatch_attempts >= self.max_pool_rejections or deadline <= self.clock():
                record.state = "FINISHED"
                record.outcome = Outcome.FAILED
                record.error = "POOL_REJECTED"
            else:
                # Re-dispatch is safe only because the pool ran and billed nothing; the same
                # pool request id is reused.
                record.state = "QUEUED"
                record.error = "POOL_REJECTED: " + str(error)
                delay = min(
                    self.reject_backoff_cap_seconds,
                    self.reject_backoff_seconds * 2 ** (record.dispatch_attempts - 1),
                )
                record.not_before = (
                    datetime.fromtimestamp(self.clock(), UTC) + timedelta(seconds=delay)
                ).isoformat()
            if record.state == "FINISHED":
                record.quiescent = True
                record.completed_at = self.now()
            await self._save(record, quota, account_quota)
            self.requests[key] = record
            self.quota = quota
            self.account_quotas[record.tenant_id] = account_quota
            # The policy saw a dispatch; the refusal ends it, holding the slot only briefly.
            self.policy.on_finish(key, self.clock(), 0)
            if record.state == "QUEUED":
                self._enqueue(record.tenant_id, record.request.root_task_id, key)

    async def _finish(self, key: str, reply: ModelReply) -> None:
        async with self.lock:
            current = self.requests[key]
            if current.state == "FINISHED":
                return
            record = current.model_copy(deep=True)
            quota = self.quota.model_copy(deep=True)
            account_quota = self._account_quota(record.tenant_id).model_copy(deep=True)
            # The dispatch window holds the worst case; settle it down to usage. A window that
            # has rolled over since dispatch keeps that worst case, which covered the request.
            actual = reply.usage.input_tokens + reply.usage.output_tokens
            for ledger in (quota, account_quota):
                ledger.reserved_units -= record.reserved_units
                ledger.worst_units -= record.worst_units
                ledger.consumed_units += reply.usage.cost_units
                ledger.inflight -= 1
                if self._in_window(record, ledger):
                    ledger.window_tokens = max(
                        0, ledger.window_tokens + actual - record.window_tokens
                    )
            record.reserved_units = 0
            record.worst_units = 0
            record.consumed_units = reply.usage.cost_units
            record.reply = reply
            record.state = "FINISHED"
            record.outcome = Outcome.CANCELLED if record.cancel_requested else Outcome.SUCCEEDED
            record.quiescent = True
            record.completed_at = self.now()
            record.error = None
            root_key = self.key(record.tenant_id, record.request.root_task_id)
            root_consumed = await self._root_consumed(root_key) + record.consumed_units
            await self._save(
                record,
                quota,
                account_quota,
                [self._root_usage(root_key, record.tenant_id, root_consumed)],
            )
            self.requests[key] = record
            self.quota = quota
            self.account_quotas[record.tenant_id] = account_quota
            self.policy.on_finish(key, self.clock(), reply.usage.cost_units)
            self.estimator.settled(
                key, record.tenant_id, record.request.agent_id, reply.usage.input_tokens,
                reply.usage.output_tokens, reply.usage.cost_units, self.clock(),
            )  # fmt: skip

    async def _root_consumed(self, root_key: str) -> int:
        row = await self.store.get("root_usage", root_key)
        return int(row["consumed_units"]) if row else 0

    @staticmethod
    def _root_usage(root_key: str, tenant: str, consumed: int) -> StoredRecord:
        return StoredRecord(
            "root_usage", root_key, tenant, {"root_key": root_key, "consumed_units": consumed}
        )

    def _prune(self) -> None:
        """Drop finished records past retention; the store keeps them for queries and dedupe."""
        if self.clock() < self._next_prune:
            return
        self._next_prune = self.clock() + 1

        def finished_at(record: GatewayRecord) -> float:
            return datetime.fromisoformat(record.completed_at or record.created_at).timestamp()

        finished = sorted(
            (finished_at(r), k) for k, r in self.requests.items() if r.state == "FINISHED"
        )
        horizon = self.clock() - self.finished_retention_seconds
        excess = len(finished) - self.max_finished_records
        for index, (at, key) in enumerate(finished):
            if index < excess or at < horizon:
                del self.requests[key]

    @staticmethod
    def pool_request_id(record: GatewayRecord) -> str:
        return record.pool_request_id or record.request.request_id

    async def reconcile(self) -> None:
        for key, record in list(self.requests.items()):
            if record.state != "UNKNOWN":
                continue
            try:
                reply = await self.provider.reconcile(
                    self.pool_request_id(record), account=record.upstream_account
                )
                if reply is not None:
                    await self._finish(key, reply)
            except Exception:
                # Query failures preserve the unresolved reservation.
                continue

    def status(self) -> JsonObject:
        return {
            "ready": self.ready,
            "quota": self.quota.model_dump(),
            "queued": sum(r.state == "QUEUED" for r in self.requests.values()),
            "unknown": sum(r.state == "UNKNOWN" for r in self.requests.values()),
            "policy": self.policy.name,
            "upstream_accounts": [a.account_id for a in self.config.pool_accounts],
            "platform_id": self.accounts.platform_id,
            "accounts": len(self.accounts.accounts),
            "pool_capacity": self.config.pool_capacity,
            "model": self.config.model,
            "pool_kind": self.config.pool_kind,
        }

    async def feedback(self, tenant: str) -> PlatformFeedback:
        async with self.lock:
            account = self.accounts.get(tenant)
            personal = self._account_quota(tenant)
            self._refresh_window()
            queued = sum(
                r.tenant_id == tenant and r.state == "QUEUED" for r in self.requests.values()
            )
            account_blocked = (
                personal.window_requests >= account.requests_per_window
                or personal.window_tokens >= account.tokens_per_window
            )
            shared_blocked = (
                self.quota.window_requests >= self.config.requests_per_window
                or self.quota.window_tokens >= self.config.tokens_per_window
            )
            remaining = max(
                0, account.budget_units - personal.consumed_units - personal.reserved_units
            )
            shared = max(
                0,
                self.config.total_budget_units
                - self.quota.consumed_units
                - self.quota.reserved_units,
            )
            # What budget admission can still take: consumption plus in-flight worst cases.
            headroom = max(
                0,
                min(
                    account.budget_units - personal.consumed_units - personal.worst_units,
                    self.config.total_budget_units
                    - self.quota.consumed_units
                    - self.quota.worst_units,
                ),
            )
            # Sending resumes once every blocking window has rolled over.
            resets = [
                quota.window_start + self.config.window_seconds - self.clock()
                for quota, blocked in ((personal, account_blocked), (self.quota, shared_blocked))
                if blocked
            ]
            retry = max(0.1, *resets) if resets else 0.2
            blocked_by: Literal["budget", "window", "queue"] | None = None
            if headroom <= 0:
                blocked_by = "budget"
            elif resets:
                blocked_by = "window"
            elif queued >= 1:
                blocked_by = "queue"
            expected = self.estimator.expected_output(tenant)
            return PlatformFeedback(
                platform_id=self.accounts.platform_id,
                account_id=tenant,
                model=self.config.model,
                pool_kind=self.config.pool_kind,
                usage_unit="mock_microcredit"
                if self.config.pool_kind == "simulated"
                else "token_unit",
                pool_capacity=self.config.pool_capacity,
                pool_inflight=self.quota.inflight,
                platform_queued=sum(r.state == "QUEUED" for r in self.requests.values()),
                platform_remaining_units=shared,
                account_budget_units=account.budget_units,
                account_consumed_units=personal.consumed_units,
                account_reserved_units=personal.reserved_units,
                account_remaining_units=remaining,
                account_inflight=personal.inflight,
                account_max_inflight=account.max_inflight,
                account_queued=queued,
                account_window_tokens_remaining=max(
                    0, account.tokens_per_window - personal.window_tokens
                ),
                can_submit=blocked_by is None,
                retry_after_seconds=retry,
                account_inflight_worst_units=personal.worst_units,
                account_admission_headroom_units=headroom,
                output_price=self.prices.output,
                cache_read_price=self.prices.cache_read,
                cache_write_price=self.prices.cache_write,
                expected_output_tokens=None if expected is None else math.ceil(expected),
                blocked_by=blocked_by,
            )

    async def close(self) -> None:
        self.ready = False
        if self._loop is not None:
            self._loop.cancel()
            await asyncio.gather(self._loop, return_exceptions=True)
        tasks = list(self._active.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.provider.close()
