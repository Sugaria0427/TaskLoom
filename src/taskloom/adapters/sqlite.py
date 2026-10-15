import asyncio
import json
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypeVar

from taskloom.domain.ports import JsonObject, StoredRecord

T = TypeVar("T")


def migrate_legacy_database(source: Path, target: Path) -> None:
    """Copy a prior ledger once; retain its original database and WAL."""
    if target.exists() or not source.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".migration")
    original = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    destination = sqlite3.connect(temporary)
    try:
        original.backup(destination)
    finally:
        destination.close()
        original.close()
    temporary.replace(target)


class SQLiteRepository:
    """One connection on one writer thread; apply commits records and outbox together."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ledger")
        self._connection: sqlite3.Connection | None = None
        self._gate = asyncio.Semaphore(64)

    def _call(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        if self._connection is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(self.path, timeout=2)
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError("Unsupported state schema version")
            self._connection.executescript(
                "CREATE TABLE IF NOT EXISTS records("
                "kind TEXT NOT NULL,id TEXT NOT NULL,tenant TEXT NOT NULL,body TEXT NOT NULL,"
                "PRIMARY KEY(kind,id));"
                "CREATE TABLE IF NOT EXISTS events("
                "seq INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT UNIQUE NOT NULL,"
                "tenant TEXT NOT NULL,body TEXT NOT NULL);"
                "PRAGMA user_version=1;"
            )
        return operation(self._connection)

    async def _run(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        async with self._gate:
            return await asyncio.get_running_loop().run_in_executor(
                self._executor, self._call, operation
            )

    async def get(self, kind: str, identity: str) -> JsonObject | None:
        def query(connection: sqlite3.Connection) -> JsonObject | None:
            row = connection.execute(
                "SELECT body FROM records WHERE kind=? AND id=?", (kind, identity)
            ).fetchone()
            result: JsonObject | None = json.loads(row[0]) if row else None
            return result

        return await self._run(query)

    async def records(self, kind: str, tenant: str | None = None) -> list[JsonObject]:
        def query(connection: sqlite3.Connection) -> list[JsonObject]:
            if tenant is None:
                cursor = connection.execute("SELECT body FROM records WHERE kind=?", (kind,))
            else:
                cursor = connection.execute(
                    "SELECT body FROM records WHERE kind=? AND tenant=?", (kind, tenant)
                )
            return [json.loads(row[0]) for row in cursor.fetchall()]

        return await self._run(query)

    async def apply(self, records: list[StoredRecord], event: JsonObject | None = None) -> None:
        def transaction(connection: sqlite3.Connection) -> None:
            with connection:
                for record in records:
                    connection.execute(
                        "INSERT INTO records(kind,id,tenant,body) VALUES(?,?,?,?) "
                        "ON CONFLICT(kind,id) DO UPDATE SET tenant=excluded.tenant,body=excluded.body",
                        (
                            record.kind,
                            record.identity,
                            record.tenant,
                            json.dumps(record.data, ensure_ascii=False),
                        ),
                    )
                if event is not None:
                    connection.execute(
                        "INSERT OR IGNORE INTO events(event_id,tenant,body) VALUES(?,?,?)",
                        (
                            event["event_id"],
                            event["tenant_id"],
                            json.dumps(event, ensure_ascii=False),
                        ),
                    )

        await self._run(transaction)

    async def events(self, tenant: str, after: int, limit: int = 100) -> list[JsonObject]:
        def query(connection: sqlite3.Connection) -> list[JsonObject]:
            rows = connection.execute(
                "SELECT seq,body FROM events WHERE tenant=? AND seq>? ORDER BY seq LIMIT ?",
                (tenant, after, min(limit, 100)),
            ).fetchall()
            result: list[JsonObject] = []
            for seq, body in rows:
                item: JsonObject = json.loads(body)
                item["event_seq"] = seq
                result.append(item)
            return result

        return await self._run(query)

    async def close(self) -> None:
        if self._connection is not None:
            await self._run(lambda connection: connection.close())
        self._executor.shutdown(wait=True)
