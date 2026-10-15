from dataclasses import dataclass
from typing import Any, Protocol

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class StoredRecord:
    kind: str
    identity: str
    tenant: str
    data: JsonObject


class Repository(Protocol):
    async def get(self, kind: str, identity: str) -> JsonObject | None: ...

    async def records(self, kind: str, tenant: str | None = None) -> list[JsonObject]: ...

    async def apply(self, records: list[StoredRecord], event: JsonObject | None = None) -> None: ...

    async def events(self, tenant: str, after: int, limit: int = 100) -> list[JsonObject]: ...

    async def close(self) -> None: ...
