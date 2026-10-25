"""Additional PR #1 review scenarios across real client settlement boundaries."""

import asyncio
import time
from collections import Counter
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest

from taskloom.adapters.sqlite import SQLiteRepository
from taskloom.client.ports import ExecutorPort, GatewayPort
from taskloom.client.service import ClientService
from taskloom.contracts.models import (
    ClientConfig,
    GatewayRecord,
    ModelReply,
    PlatformFeedback,
    TaskCreate,
    Usage,
    digest,
)
from taskloom.domain.model import Outcome


def feedback():
    return PlatformFeedback(
        platform_id="platform",
        account_id="alice",
        model="fixed",
        pool_capacity=2,
        pool_inflight=0,
        platform_queued=0,
        platform_remaining_units=1000000,
        account_budget_units=100000,
        account_consumed_units=0,
        account_reserved_units=0,
        account_remaining_units=100000,
        account_inflight=0,
        account_max_inflight=2,
        account_queued=0,
        account_window_tokens_remaining=100000,
        can_submit=True,
        retry_after_seconds=0.1,
        observed_at=datetime.fromtimestamp(time.time(), UTC).isoformat(),
    )


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_a_delayed_unknown_reply_cannot_undo_a_final_settlement(tmp_path):
    store = SQLiteRepository(tmp_path / "client.db")
    gateway = AsyncMock(spec=GatewayPort)
    gateway.feedback.side_effect = feedback
    first_query_started = asyncio.Event()
    release_stale_query = asyncio.Event()
    rows = {}
    queries = Counter()

    async def submit(req):
        row = GatewayRecord(
            tenant_id="alice",
            request=req,
            payload_hash=digest(req.model_dump()),
            state="DISPATCH_INTENT",
            outcome=Outcome.PENDING,
            reserved_units=req.reserve_units,
            quiescent=False,
        )
        rows[req.request_id] = row
        return row.model_copy(deep=True)

    async def query(request_id):
        queries[request_id] += 1
        old = rows[request_id].model_copy(deep=True)
        if queries[request_id] == 1:
            # The model polling path received UNKNOWN before reconciliation, but its
            # response is delayed in transit until the final result has been saved.
            old.state = "UNKNOWN"
            old.outcome = Outcome.UNKNOWN
            first_query_started.set()
            await release_stale_query.wait()
            return old
        return old.model_copy(
            update={
                "state": "FINISHED",
                "outcome": Outcome.SUCCEEDED,
                "quiescent": True,
                "reserved_units": 0,
                "consumed_units": 10,
                "reply": ModelReply(
                    content="done",
                    usage=Usage(
                        input_tokens=1,
                        output_tokens=1,
                        cost_units=10,
                    ),
                ),
            }
        )

    gateway.submit.side_effect = submit
    gateway.query.side_effect = query
    service = ClientService(
        store, gateway, AsyncMock(spec=ExecutorPort), tmp_path, ClientConfig(max_inflight=1)
    )
    service.models.settle = Mock(wraps=service.models.settle)
    await service.start()
    # Drive reconciliation explicitly; the production reconciler uses this same method.
    service.reconciler.cancel()
    await asyncio.gather(service.reconciler, return_exceptions=True)
    try:
        task = await service.create(TaskCreate(documents={"a.txt": "one two"}))
        await asyncio.wait_for(first_query_started.wait(), 2)
        await service.reconcile()
        before = next(iter(service.attempts.values())).model_copy(deep=True)
        assert before.settled and before.consumed_units == 10
        release_stale_query.set()
        await until(lambda: task.task_id not in service.runners)
        after = next(iter(service.attempts.values())).model_copy(deep=True)
        await service.reconcile()
        print(
            {
                "before": {
                    "settled": before.settled,
                    "consumed": before.consumed_units,
                    "outcome": str(before.outcome),
                },
                "after_delayed_response": {
                    "settled": after.settled,
                    "consumed": after.consumed_units,
                    "outcome": str(after.outcome),
                },
                "fairness_settlement_count": service.models.settle.call_count,
            }
        )
        assert after.settled and after.consumed_units == 10, "Final settlement was overwritten"
        assert service.models.settle.call_count == 1, "Same attempt refunded more than once"
    finally:
        release_stale_query.set()
        await service.close()
        await store.close()
