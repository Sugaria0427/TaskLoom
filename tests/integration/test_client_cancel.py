import asyncio
from unittest.mock import AsyncMock

import pytest

from taskloom.adapters.sqlite import SQLiteRepository
from taskloom.client.ports import ExecutorPort, GatewayPort
from taskloom.client.service import ClientService
from taskloom.contracts.models import ClientConfig, GatewayRecord, TaskCreate
from taskloom.domain.model import Outcome, WorkState


async def test_cancel_before_model_dispatch_closes_durable_attempt(
    tmp_path, platform_feedback
) -> None:
    repository = SQLiteRepository(tmp_path / "client.db")
    gateway = AsyncMock(spec=GatewayPort)
    gateway.feedback.return_value = platform_feedback
    executor = AsyncMock(spec=ExecutorPort)
    service = ClientService(repository, gateway, executor, tmp_path, ClientConfig(max_inflight=1))
    await service.start()
    # 平台侧不接受提交，模型请求会停在调度队列里等，从而复现"还没派发就被取消"。
    gateway.feedback.return_value = platform_feedback.model_copy(update={"can_submit": False})
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
        assert not service.models.pending
        assert service.models.in_flight == 0
    finally:
        await service.close()
        await repository.close()


@pytest.mark.parametrize("reply_pending", [False, True])
async def test_cancel_after_model_dispatch_reaches_the_execution(
    tmp_path, platform_feedback, reply_pending
) -> None:
    """复现审查意见 2.2：取消必须传到已经派发的模型执行路径。

    平台已经受理请求、执行任务在轮询结果时取消根任务，取消必须打进
    执行路径：网关收到取消、本地在途归零，预留保留待核对而不是被吞掉。
    """
    repository = SQLiteRepository(tmp_path / "client.db")
    gateway = AsyncMock(spec=GatewayPort)
    gateway.feedback.return_value = platform_feedback
    executor = AsyncMock(spec=ExecutorPort)
    service = ClientService(repository, gateway, executor, tmp_path, ClientConfig(max_inflight=1))

    request_record = None
    accepted = asyncio.Event()

    async def submit(request):
        nonlocal request_record
        request_record = GatewayRecord(
            tenant_id=request.root_task_id,
            request=request,
            payload_hash="hash",
            state="DISPATCHED",
            outcome=Outcome.PENDING,
            reserved_units=request.reserve_units,
        )
        accepted.set()
        if reply_pending:
            # The platform admitted the request but its submit response is still in transit.
            await asyncio.Event().wait()
        return request_record

    gateway.submit.side_effect = submit
    gateway.query.side_effect = lambda request_id: request_record
    gateway.cancel.side_effect = lambda request_id: request_record.model_copy(
        update={"cancel_requested": True}
    )

    await service.start()
    try:
        task = await service.create(TaskCreate(documents={"a.txt": "one two"}))
        # Occupying a scheduler slot precedes execution; synchronize on actual admission.
        await asyncio.wait_for(accepted.wait(), timeout=3)

        cancelled = await service.cancel(task.task_id)
        gateway.cancel.assert_awaited()
        assert service.models.in_flight == 0
        assert not service.models.pending
        # 平台还没确认结果，预留保留，任务停在待核对而不是被当成完成。
        assert cancelled.reserved_units > 0
        assert cancelled.state == WorkState.RECONCILING
        assert cancelled.outcome == Outcome.CANCELLED
        attempts = await repository.records("attempt", "alice")
        assert len(attempts) == 1
        assert attempts[0]["state"] == WorkState.RECONCILING
        assert attempts[0]["reserved_units"] > 0
        assert not attempts[0]["settled"]
    finally:
        await service.close()
        await repository.close()
