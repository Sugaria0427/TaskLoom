import asyncio
from unittest.mock import AsyncMock

from taskloom.adapters.sqlite import SQLiteRepository
from taskloom.client.ports import ExecutorPort, GatewayPort
from taskloom.client.service import ClientService
from taskloom.contracts.models import ClientConfig, TaskCreate
from taskloom.domain.model import Outcome, Resources, WorkState


async def test_cancel_before_model_dispatch_closes_durable_attempt(
    tmp_path, platform_feedback
) -> None:
    repository = SQLiteRepository(tmp_path / "client.db")
    gateway = AsyncMock(spec=GatewayPort)
    gateway.feedback.return_value = platform_feedback
    executor = AsyncMock(spec=ExecutorPort)
    service = ClientService(repository, gateway, executor, tmp_path, ClientConfig(max_inflight=1))
    await service.start()
    service.models.pool.reserve("other-request", Resources(slots=1))
    try:
        task = await service.create(TaskCreate(documents={"a.txt": "one two"}))
        for _ in range(3000):
            if service.resources()["queued_models"]:
                break
            await asyncio.sleep(0.001)
        else:
            raise AssertionError("Task did not reach the model admission queue")
        cancelled = await service.cancel(task.task_id)
        assert cancelled.outcome == Outcome.CANCELLED
        assert cancelled.quiescent and cancelled.reserved_units == 0
        assert cancelled.completed_at is not None
        attempts = await repository.records("attempt", "alice")
        assert len(attempts) == 1
        assert attempts[0]["outcome"] == Outcome.CANCELLED
        assert attempts[0]["state"] == WorkState.FINISHED
        assert attempts[0]["settled"]
        gateway.submit.assert_not_awaited()
        assert not len(service.models.queue)
        assert service.models.pool.release("other-request")
        assert service.models.pool.available.slots == 1
    finally:
        await service.close()
        await repository.close()
