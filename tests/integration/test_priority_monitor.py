import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from taskloom.adapters.sqlite import SQLiteRepository
from taskloom.bootstrap.apps import client_app
from taskloom.client.dispatcher import Dispatcher
from taskloom.client.service import ClientService
from taskloom.contracts.models import ClientConfig, TaskCreate
from taskloom.domain.model import DomainError, Resources


def test_task_priority_defaults_and_validates() -> None:
    assert TaskCreate().priority == "normal"
    assert TaskCreate(priority="high").priority == "high"
    with pytest.raises(ValueError):
        TaskCreate(priority="immediate")


async def test_high_priority_queued_work_runs_first() -> None:
    dispatcher = Dispatcher(Resources(slots=1))
    await dispatcher.start()
    started, release = asyncio.Event(), asyncio.Event()
    order: list[str] = []

    async def blocking() -> None:
        started.set()
        await release.wait()

    async def run(name: str) -> None:
        order.append(name)

    try:
        first = asyncio.create_task(
            dispatcher.submit("first", "root-1", Resources(slots=1), blocking)
        )
        await started.wait()
        low = asyncio.create_task(
            dispatcher.submit(
                "low", "root-2", Resources(slots=1), lambda: run("low"), priority="low"
            )
        )
        high = asyncio.create_task(
            dispatcher.submit(
                "high", "root-3", Resources(slots=1), lambda: run("high"), priority="high"
            )
        )
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, low, high)
        assert order == ["high", "low"]
    finally:
        await dispatcher.close()


async def test_reprioritized_waiting_work_moves_without_restarting() -> None:
    dispatcher = Dispatcher(Resources(slots=1))
    await dispatcher.start()
    started, release = asyncio.Event(), asyncio.Event()
    order: list[str] = []

    async def blocking() -> None:
        started.set()
        await release.wait()

    async def run(name: str) -> None:
        order.append(name)

    try:
        first = asyncio.create_task(
            dispatcher.submit("first", "root-1", Resources(slots=1), blocking)
        )
        await started.wait()
        normal = asyncio.create_task(
            dispatcher.submit("normal", "root-2", Resources(slots=1), lambda: run("normal"))
        )
        changed = asyncio.create_task(
            dispatcher.submit(
                "changed", "root-3", Resources(slots=1), lambda: run("changed"), priority="low"
            )
        )
        await asyncio.sleep(0)
        assert dispatcher.set_priority("changed", "high")
        release.set()
        await asyncio.gather(first, normal, changed)
        assert order == ["changed", "normal"]
    finally:
        await dispatcher.close()


async def test_low_priority_gets_turn_under_sustained_high_load() -> None:
    dispatcher = Dispatcher(Resources(slots=1))
    await dispatcher.start()
    started, release = asyncio.Event(), asyncio.Event()
    order: list[str] = []

    async def blocking() -> None:
        started.set()
        await release.wait()

    async def run(name: str) -> None:
        order.append(name)

    try:
        first = asyncio.create_task(
            dispatcher.submit("first", "root-1", Resources(slots=1), blocking)
        )
        await started.wait()
        high = [
            asyncio.create_task(
                dispatcher.submit(
                    f"high-{i}",
                    f"root-{i + 2}",
                    Resources(slots=1),
                    lambda i=i: run(f"high-{i}"),
                    priority="high",
                )
            )
            for i in range(20)
        ]
        low = asyncio.create_task(
            dispatcher.submit(
                "low", "root-low", Resources(slots=1), lambda: run("low"), priority="low"
            )
        )
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, *high, low)
        assert order.index("low") < 20
    finally:
        await dispatcher.close()


async def test_priority_update_is_durable_and_only_allowed_while_pending(
    tmp_path, platform_feedback
) -> None:
    repository = SQLiteRepository(tmp_path / "client.db")
    gateway = AsyncMock()
    gateway.feedback.return_value = platform_feedback
    service = ClientService(
        repository, gateway, AsyncMock(), tmp_path, ClientConfig(max_inflight=1)
    )
    await service.start()
    gateway.feedback.return_value = platform_feedback.model_copy(update={"can_submit": False})
    try:
        task = await service.create(TaskCreate(priority="low"))
        updated = await service.update_priority(task.task_id, "high")
        assert updated.specification.priority == "high"
        assert (await repository.get("task", task.task_id))["specification"]["priority"] == "high"
        await service.cancel(task.task_id)
        with pytest.raises(DomainError):
            await service.update_priority(task.task_id, "normal")
    finally:
        await service.close()
        await repository.close()


async def test_priority_update_route_requires_local_control_header(tmp_path) -> None:
    app = client_app(tmp_path, ClientConfig())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        denied = await client.post("/api/v1/tasks/missing/priority", json={"priority": "high"})
        assert denied.status_code == 403
        accepted = await client.post(
            "/api/v1/tasks/missing/priority",
            json={"priority": "high"},
            headers={"X-TaskLoom-Local": "1"},
        )
        assert accepted.status_code == 404
        assert accepted.json()["error"]["code"] == "NOT_FOUND"
