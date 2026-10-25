import asyncio
from unittest.mock import AsyncMock

from taskloom.adapters.sqlite import SQLiteRepository
from taskloom.client.ports import ExecutorPort, GatewayPort
from taskloom.client.service import ClientService
from taskloom.contracts.models import (
    ClientConfig,
    GatewayRecord,
    ModelReply,
    Outcome,
    TaskCreate,
    Usage,
)
from taskloom.domain.model import WorkState


async def test_settlement_uses_the_original_reservation(tmp_path, platform_feedback) -> None:
    """复现审查意见 2.3：校正要用原预扣量，不能用平台结算后清零的值。

    请求预扣 1000、平台报实际消费 10。平台完成记录里的 ``reserved_units``
    已经是 0，客户端必须回到原请求里取 1000，按 ``settle(root, 1000, 10)``
    退回 990；拿 0 去校正会反过来多扣 10。
    """
    repository = SQLiteRepository(tmp_path / "client.db")
    gateway = AsyncMock(spec=GatewayPort)
    gateway.feedback.return_value = platform_feedback
    executor = AsyncMock(spec=ExecutorPort)
    service = ClientService(repository, gateway, executor, tmp_path, ClientConfig(max_inflight=1))

    finished = None

    async def submit(request):
        nonlocal finished
        finished = GatewayRecord(
            tenant_id=request.root_task_id,
            request=request,
            payload_hash="hash",
            state="QUEUED",
            outcome=Outcome.PENDING,
            reserved_units=request.reserve_units,
        )
        return finished

    async def query(request_id):
        return finished.model_copy(
            update={
                "state": "FINISHED",
                "outcome": Outcome.SUCCEEDED,
                "quiescent": True,
                "reply": ModelReply(
                    content="ok",
                    tool_calls=[],
                    usage=Usage(input_tokens=1, output_tokens=1, cost_units=10),
                ),
                "reserved_units": 0,
                "consumed_units": 10,
            }
        )

    gateway.submit.side_effect = submit
    gateway.query.side_effect = query

    settle_calls: list[tuple[str, int, int]] = []
    original_settle = service.models.settle

    def spy(owner: str, reserved: int, consumed: int) -> None:
        settle_calls.append((owner, reserved, consumed))
        original_settle(owner, reserved, consumed)

    service.models.settle = spy  # type: ignore[method-assign]

    await service.start()
    try:
        task = await service.create(TaskCreate(documents={"a.txt": "one two"}))
        for _ in range(3000):
            if settle_calls:
                break
            await asyncio.sleep(0.001)
        else:
            raise AssertionError("Model request was never settled")

        assert len(settle_calls) == 1
        owner, reserved, consumed = settle_calls[0]
        assert owner == task.task_id
        assert finished is not None
        assert reserved == finished.request.reserve_units
        assert consumed == 10

        # 重复核对不能把同一份额度退第二遍。
        await service.reconcile()
        assert len(settle_calls) == 1
        attempts = await repository.records("attempt", "alice")
        assert attempts[0]["settled"]
        assert attempts[0]["state"] == WorkState.FINISHED
    finally:
        await service.close()
        await repository.close()
