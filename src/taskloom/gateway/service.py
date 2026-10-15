import asyncio
import time
from datetime import datetime

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
    utc_now,
)
from taskloom.domain.model import DomainError, Outcome
from taskloom.domain.ports import Repository, StoredRecord
from taskloom.gateway.accounts import AccountService
from taskloom.gateway.ports import ModelProvider
from taskloom.policies.fair import FairQueue


class GatewayService:
    def __init__(self, store: Repository, provider: ModelProvider, config: PlatformConfig) -> None:
        self.store = store
        self.provider = provider
        self.config = config
        self.requests: dict[str, GatewayRecord] = {}
        self.quota = QuotaState()
        self.accounts = AccountService(store, config)
        self.account_quotas: dict[str, QuotaState] = {}
        self.queue: FairQueue[str] = FairQueue()
        self._roots: dict[str, FairQueue[str]] = {}
        self.lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self._loop: asyncio.Task[None] | None = None
        self._active: dict[str, asyncio.Task[None]] = {}
        self.ready = False

    @staticmethod
    def key(tenant: str, request_id: str) -> str:
        return tenant + ":" + request_id

    async def start(self) -> None:
        await self.accounts.start()
        quota = await self.store.get("quota", "shared")
        self.quota = QuotaState.model_validate(quota) if quota else QuotaState()
        for item in await self.store.records("account_quota"):
            self.account_quotas[item["account_id"]] = QuotaState.model_validate(item["quota"])
        for row in await self.store.records("request"):
            record = GatewayRecord.model_validate(row)
            key = self.key(record.tenant_id, record.request.request_id)
            self.requests[key] = record
            if record.state == "QUEUED":
                self._enqueue(record.tenant_id, record.request.root_task_id, key)
            elif record.outcome == Outcome.PENDING:
                record.state = "UNKNOWN"
                record.outcome = Outcome.UNKNOWN
                record.error = "Dispatch interrupted; reconciliation required"
                await self._save(record)
        await self.reconcile()
        self.ready = True
        self._loop = asyncio.create_task(self._schedule(), name="gateway-dispatch")

    def _enqueue(self, tenant: str, root: str, key: str) -> None:
        if tenant not in self._roots:
            self._roots[tenant] = FairQueue()
            self.queue.push(tenant, tenant)
        self._roots[tenant].push(root, key)
        self.wake.set()

    def _candidate(self) -> str | None:
        # Outer round-robin is per tenant; inner is per registered root task.
        for _ in range(len(self._roots)):
            tenant = self.queue.pop()
            if tenant is None:
                return None
            inner = self._roots[tenant]
            key = inner.pop(lambda item: self._feasible(self.requests[item]))
            if len(inner):
                self.queue.push(tenant, tenant)
            else:
                del self._roots[tenant]
            if key is not None:
                return key
        return None

    async def register_root(self, tenant: str, root: RootRegistration) -> None:
        self.accounts.get(tenant)
        async with self.lock:
            key = self.key(tenant, root.root_task_id)
            previous = await self.store.get("root", key)
            data = root.model_dump()
            data["tenant_id"] = tenant
            if previous is not None and previous != data:
                raise DomainError("ROOT_CONFLICT", "Root task metadata changed")
            if previous is None and len(await self.store.records("root")) >= 1024:
                raise DomainError("QUEUE_FULL", "Root registry capacity exhausted", True)
            await self.store.apply([StoredRecord("root", key, tenant, data)])

    async def submit(self, tenant: str, request: ModelRequest) -> GatewayRecord:
        async with self.lock:
            account = self.accounts.get(tenant)
            key = self.key(tenant, request.request_id)
            payload_hash = digest(request.model_dump(mode="json"))
            previous = self.requests.get(key)
            if previous is not None:
                if previous.payload_hash != payload_hash:
                    raise DomainError(
                        "DUPLICATE_ID_CONFLICT", "Request identity has different content"
                    )
                return previous.model_copy(deep=True)
            root = await self.store.get("root", self.key(tenant, request.root_task_id))
            if root is None:
                raise DomainError("ROOT_NOT_REGISTERED", "Register this user's root task first")
            if request.reserve_units > min(self.config.total_budget_units, root["budget_units"]):
                raise DomainError("REQUEST_TOO_LARGE", "Request exceeds the budget domain")
            if request.reserve_units > self.config.tokens_per_window:
                raise DomainError("REQUEST_TOO_LARGE", "Request exceeds the token window capacity")
            if request.reserve_units > min(account.budget_units, account.tokens_per_window):
                raise DomainError("REQUEST_TOO_LARGE", "Request exceeds the account allowance")
            if datetime.fromisoformat(request.deadline_at).timestamp() <= time.time():
                raise DomainError("DEADLINE_EXPIRED", "Request deadline already expired")
            queued = [r for r in self.requests.values() if r.state == "QUEUED"]
            size = sum(len(r.request.model_dump_json().encode()) for r in queued)
            history_size = sum(
                len(r.request.model_dump_json().encode()) for r in self.requests.values()
            )
            if (
                len(queued) >= self.config.max_queue
                or sum(r.tenant_id == tenant for r in queued) >= self.config.max_tenant_queue
                or size + len(request.model_dump_json().encode()) > self.config.max_queue_bytes
                or len(self.requests) >= 1024
                or history_size + len(request.model_dump_json().encode())
                > self.config.max_history_bytes
            ):
                raise DomainError("QUEUE_FULL", "Admission capacity exhausted", True)
            record = GatewayRecord(tenant_id=tenant, request=request, payload_hash=payload_hash)
            await self._save(record)
            self.requests[key] = record
            self._enqueue(tenant, request.root_task_id, key)
            return record.model_copy(deep=True)

    def get(self, tenant: str, request_id: str) -> GatewayRecord:
        record = self.requests.get(self.key(tenant, request_id))
        if record is None:
            raise DomainError("NOT_FOUND", "Request not found")
        return record.model_copy(deep=True)

    async def cancel(self, tenant: str, request_id: str) -> GatewayRecord:
        async with self.lock:
            record = self.get(tenant, request_id)
            record.cancel_requested = True
            if record.state == "QUEUED":
                record.state = "FINISHED"
                record.outcome = Outcome.CANCELLED
                record.quiescent = True
                record.completed_at = utc_now()
            await self._save(record)
            self.requests[self.key(tenant, request_id)] = record
            self.wake.set()
            return record.model_copy(deep=True)

    def _refresh_window(self) -> None:
        for quota in [self.quota, *self.account_quotas.values()]:
            if time.time() >= quota.window_start + self.config.window_seconds:
                quota.window_start = time.time()
                quota.window_requests = 0
                quota.window_tokens = 0

    def _account_quota(self, tenant: str) -> QuotaState:
        if tenant not in self.account_quotas:
            records = [r for r in self.requests.values() if r.tenant_id == tenant]
            self.account_quotas[tenant] = QuotaState(
                window_start=self.quota.window_start or time.time(),
                consumed_units=sum(r.consumed_units for r in records),
                reserved_units=sum(r.reserved_units for r in records),
                inflight=sum(bool(r.reserved_units) for r in records),
            )
        return self.account_quotas[tenant]

    def _feasible(self, record: GatewayRecord) -> bool:
        if record.state != "QUEUED":
            return True
        account = self.accounts.accounts.get(record.tenant_id)
        if account is None:
            return False
        personal = self._account_quota(record.tenant_id)
        return (
            self.quota.inflight < self.config.pool_capacity
            and self.quota.window_requests < self.config.requests_per_window
            and self.quota.window_tokens + record.request.reserve_units
            <= self.config.tokens_per_window
            and self.quota.consumed_units + self.quota.reserved_units + record.request.reserve_units
            <= self.config.total_budget_units
            and personal.inflight < account.max_inflight
            and personal.window_requests < account.requests_per_window
            and personal.window_tokens + record.request.reserve_units <= account.tokens_per_window
            and personal.consumed_units + personal.reserved_units + record.request.reserve_units
            <= account.budget_units
        )

    async def _save(
        self,
        record: GatewayRecord,
        quota: QuotaState | None = None,
        account_quota: QuotaState | None = None,
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
        await self.store.apply(
            records,
            {
                "schema_version": 1,
                "event_id": identity(),
                "tenant_id": record.tenant_id,
                "root_task_id": record.request.root_task_id,
                "attempt_id": record.request.attempt_id,
                "type": "request." + record.state.lower(),
                "occurred_at": utc_now(),
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
                        datetime.fromisoformat(old.request.deadline_at).timestamp() <= time.time()
                    )
                    exhausted = (
                        self.quota.consumed_units + old.request.reserve_units
                        > self.config.total_budget_units
                    )
                    account = self.accounts.accounts.get(old.tenant_id)
                    if account is not None:
                        exhausted = exhausted or (
                            self._account_quota(old.tenant_id).consumed_units
                            + old.request.reserve_units
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
                while self.quota.inflight < self.config.pool_capacity:
                    key = self._candidate()
                    if key is None:
                        break
                    record = self.requests[key].model_copy(deep=True)
                    if record.state != "QUEUED":
                        continue
                    if datetime.fromisoformat(record.request.deadline_at).timestamp() < time.time():
                        record.outcome = Outcome.FAILED
                        record.state = "FINISHED"
                        record.error = "DEADLINE_EXPIRED"
                        record.quiescent = True
                        await self._save(record)
                        self.requests[key] = record
                        continue
                    root_records = [
                        r
                        for r in self.requests.values()
                        if r.tenant_id == record.tenant_id
                        and r.request.root_task_id == record.request.root_task_id
                    ]
                    root = await self.store.get(
                        "root", self.key(record.tenant_id, record.request.root_task_id)
                    )
                    root_used = sum(r.reserved_units + r.consumed_units for r in root_records)
                    if (
                        root is None
                        or root_used + record.request.reserve_units > root["budget_units"]
                    ):
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
                    record.dispatched_at = utc_now()
                    record.reserved_units = record.request.reserve_units
                    quota.reserved_units += record.reserved_units
                    quota.inflight += 1
                    quota.window_requests += 1
                    quota.window_tokens += record.request.reserve_units
                    account_quota.reserved_units += record.reserved_units
                    account_quota.inflight += 1
                    account_quota.window_requests += 1
                    account_quota.window_tokens += record.request.reserve_units
                    await self._save(record, quota, account_quota)
                    self.requests[key] = record
                    self.quota = quota
                    self.account_quotas[record.tenant_id] = account_quota
                    self._active[key] = asyncio.create_task(self._dispatch(key))
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
            reply = await self.provider.generate(upstream)
            await self._finish(key, reply)
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

    async def _finish(self, key: str, reply: ModelReply) -> None:
        async with self.lock:
            current = self.requests[key]
            if current.state == "FINISHED":
                return
            record = current.model_copy(deep=True)
            quota = self.quota.model_copy(deep=True)
            account_quota = self._account_quota(record.tenant_id).model_copy(deep=True)
            quota.reserved_units -= record.reserved_units
            quota.consumed_units += reply.usage.cost_units
            quota.inflight -= 1
            account_quota.reserved_units -= record.reserved_units
            account_quota.consumed_units += reply.usage.cost_units
            account_quota.inflight -= 1
            record.reserved_units = 0
            record.consumed_units = reply.usage.cost_units
            record.reply = reply
            record.state = "FINISHED"
            record.outcome = Outcome.CANCELLED if record.cancel_requested else Outcome.SUCCEEDED
            record.quiescent = True
            record.completed_at = utc_now()
            record.error = None
            await self._save(record, quota, account_quota)
            self.requests[key] = record
            self.quota = quota
            self.account_quotas[record.tenant_id] = account_quota

    @staticmethod
    def pool_request_id(record: GatewayRecord) -> str:
        return record.pool_request_id or record.request.request_id

    async def reconcile(self) -> None:
        for key, record in list(self.requests.items()):
            if record.state != "UNKNOWN":
                continue
            try:
                reply = await self.provider.reconcile(self.pool_request_id(record))
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
            "policy": "tenant/root round-robin",
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
            window_blocked = (
                personal.window_requests >= account.requests_per_window
                or personal.window_tokens >= account.tokens_per_window
                or self.quota.window_requests >= self.config.requests_per_window
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
            retry = (
                max(0.1, self.quota.window_start + self.config.window_seconds - time.time())
                if window_blocked
                else 0.2
            )
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
                can_submit=not window_blocked and queued < 1 and remaining > 0 and shared > 0,
                retry_after_seconds=retry,
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
