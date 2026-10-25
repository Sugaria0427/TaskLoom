import asyncio
import copy
import json

from taskloom.contracts.models import AccountRecord, GatewayRecord
from taskloom.domain.ports import JsonObject, StoredRecord


class MemoryRepository:
    """Repository port without threads; JSON round-trips mirror SQLite serialization."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], tuple[str, JsonObject]] = {}
        self.event_log: list[JsonObject] = []
        self._event_ids: set[str] = set()
        self.writes = 0
        # Largest reservation each request row ever carried; the gateway zeroes it at
        # settlement, so this is the only record of what it actually reserved.
        self.reservations: dict[str, int] = {}
        # Every request row as last written; the gateway prunes finished ones from memory.
        self.requests: dict[str, GatewayRecord] = {}

    async def get(self, kind: str, identity: str) -> JsonObject | None:
        row = self.rows.get((kind, identity))
        return copy.deepcopy(row[1]) if row else None

    async def records(self, kind: str, tenant: str | None = None) -> list[JsonObject]:
        return [
            copy.deepcopy(data)
            for (row_kind, _), (row_tenant, data) in self.rows.items()
            if row_kind == kind and (tenant is None or row_tenant == tenant)
        ]

    async def apply(self, records: list[StoredRecord], event: JsonObject | None = None) -> None:
        staged = [(r.kind, r.identity, r.tenant, json.loads(json.dumps(r.data))) for r in records]
        # SQLite commits on its writer thread; keep the same suspension point for interleavings.
        await asyncio.sleep(0)
        for kind, identity, tenant, data in staged:
            self.rows[(kind, identity)] = (tenant, data)
            if kind == "request" and data.get("reserved_units"):
                self.reservations[identity] = max(
                    self.reservations.get(identity, 0), data["reserved_units"]
                )
            if kind == "request":
                self.requests[identity] = GatewayRecord.model_validate(data)
        if event is not None and event["event_id"] not in self._event_ids:
            self._event_ids.add(event["event_id"])
            self.event_log.append(
                {**json.loads(json.dumps(event)), "event_seq": len(self.event_log) + 1}
            )
        self.writes += 1

    async def events(self, tenant: str, after: int, limit: int = 100) -> list[JsonObject]:
        selected = [
            copy.deepcopy(e)
            for e in self.event_log
            if e["tenant_id"] == tenant and e["event_seq"] > after
        ]
        return selected[: min(limit, 100)]

    async def close(self) -> None:
        pass

    def seed_account(self, account: AccountRecord) -> None:
        """Bypass PBKDF2 registration, whose worker thread cannot run on the virtual loop."""
        self.rows[("account", account.account_id)] = (account.account_id, account.model_dump())
