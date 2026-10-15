import asyncio

import pytest

from taskloom.adapters.sqlite import SQLiteRepository
from taskloom.client.dispatcher import Dispatcher, QuarantinedExecution
from taskloom.domain.model import Resources
from taskloom.domain.ports import StoredRecord


async def test_sqlite_batch_rolls_back_and_outbox_deduplicates(tmp_path) -> None:
    repository = SQLiteRepository(tmp_path / "ledger.db")
    event = {"event_id": "same", "tenant_id": "alice", "type": "completed"}
    try:
        await repository.apply([StoredRecord("task", "a", "alice", {"state": "ready"})], event)
        with pytest.raises(TypeError):
            await repository.apply(
                [
                    StoredRecord("task", "a", "alice", {"state": "bad"}),
                    StoredRecord("task", "b", "alice", {"not_json": {1, 2}}),
                ]
            )
        assert (await repository.get("task", "a"))["state"] == "ready"
        assert await repository.get("task", "b") is None
        await repository.apply([], event)
        assert len(await repository.events("alice", 0)) == 1
    finally:
        await repository.close()
    reopened = SQLiteRepository(tmp_path / "ledger.db")
    try:
        assert (await reopened.get("task", "a"))["state"] == "ready"
    finally:
        await reopened.close()


async def test_cancelled_queued_job_never_executes() -> None:
    dispatcher = Dispatcher(Resources(slots=1))
    await dispatcher.start()
    first_started = asyncio.Event()
    release = asyncio.Event()
    seen = []

    async def first() -> int:
        first_started.set()
        await release.wait()
        return 1

    async def second() -> int:
        seen.append("should not run")
        return 2

    a = asyncio.create_task(dispatcher.submit("a", "root-a", Resources(slots=1), first))
    await first_started.wait()
    b = asyncio.create_task(dispatcher.submit("b", "root-b", Resources(slots=1), second))
    await asyncio.sleep(0)
    b.cancel()
    await asyncio.gather(b, return_exceptions=True)
    release.set()
    assert await a == 1
    await asyncio.sleep(0)
    assert not seen
    assert dispatcher.pool.available.slots == 1
    await dispatcher.close()


async def test_unknown_physical_stop_retains_reservation() -> None:
    dispatcher = Dispatcher(Resources(slots=1))
    await dispatcher.start()

    async def unknown() -> int:
        raise QuarantinedExecution("No physical stop evidence")

    try:
        with pytest.raises(QuarantinedExecution):
            await dispatcher.submit("unknown", "root", Resources(slots=1), unknown)
        assert dispatcher.pool.available.slots == 0
        assert dispatcher.quarantined == {"unknown"}
    finally:
        await dispatcher.close()
