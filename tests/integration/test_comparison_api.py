import asyncio
import threading
from unittest.mock import AsyncMock

import httpx
import pytest

from taskloom.adapters.sqlite import SQLiteRepository
from taskloom.bootstrap.apps import client_app
from taskloom.contracts.models import ClientConfig
from taskloom.domain.model import DomainError
from taskloom.domain.ports import StoredRecord
from taskloom.experiments.comparison import ComparisonSpec
from taskloom.experiments.service import ComparisonService


async def until_finished(service, run_id):
    assert service.job is not None
    await asyncio.wait_for(asyncio.shield(service.job), timeout=10)
    record = await service.get(run_id)
    assert record["status"] == "FINISHED", record
    return record


async def test_result_ring_persists_and_reclaims_only_old_results(tmp_path):
    store = SQLiteRepository(tmp_path / "comparisons.db")
    service = ComparisonService(store, "alice")
    service.history_limit = 2
    await service.start()
    try:
        runs = []
        for seed in range(3):
            run = await service.submit(
                ComparisonSpec(days=1, tasks_per_day=8, repeats=1, seed=seed)
            )
            runs.append(await until_finished(service, run["run_id"]))
        assert len(await store.records("comparison")) == 2
        with pytest.raises(DomainError, match="不存在"):
            await service.get(runs[0]["run_id"])
        restarted = ComparisonService(store, "alice")
        await restarted.start()
        assert (await restarted.get(runs[-1]["run_id"]))["result"] == runs[-1]["result"]
        assert len(await restarted.history()) == 2
    finally:
        await service.close()
        await store.close()


async def test_single_flight_rejects_overlapping_work(tmp_path, monkeypatch):
    import taskloom.experiments.service as module

    gate = threading.Event()

    def controlled(spec):
        assert gate.wait(5)
        return {"source": "test-controlled"}

    monkeypatch.setattr(module, "compare", controlled)
    store = SQLiteRepository(tmp_path / "comparisons.db")
    service = ComparisonService(store, "alice")
    await service.start()
    try:
        run = await service.submit(ComparisonSpec(days=1, tasks_per_day=8, repeats=1))
        with pytest.raises(DomainError, match="正在计算"):
            await service.submit(ComparisonSpec())
        gate.set()
        await until_finished(service, run["run_id"])
    finally:
        gate.set()
        await service.close()
        await store.close()


async def test_restart_marks_unfinished_calculation_as_interrupted(tmp_path):
    store = SQLiteRepository(tmp_path / "comparisons.db")
    await store.apply(
        [
            StoredRecord(
                "comparison",
                "1",
                "alice",
                {
                    "sequence": 1,
                    "slot": "1",
                    "run_id": "interrupted-run",
                    "status": "RUNNING",
                    "spec": ComparisonSpec().model_dump(),
                    "result": None,
                },
            )
        ]
    )
    service = ComparisonService(store, "alice")
    try:
        await service.start()
        record = await service.get("interrupted-run")
        assert record["status"] == "FAILED" and record["completed_at"]
        assert record["result"] is None
        assert service.job is None
    finally:
        await service.close()
        await store.close()


async def test_local_api_exports_simulation_and_real_records_separately(tmp_path):
    app = client_app(tmp_path, ClientConfig())
    app.state.service.start = AsyncMock()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            assert (await http.get("/experiments")).status_code == 200
            assert (await http.post("/api/v1/experiments", json={})).status_code == 403
            headers = {"X-TaskLoom-Local": "1"}
            invalid = await http.post("/api/v1/experiments", headers=headers, json={"days": 1000})
            assert invalid.status_code == 422
            response = await http.post(
                "/api/v1/experiments",
                headers=headers,
                json={"days": 1, "tasks_per_day": 8, "repeats": 1},
            )
            assert response.status_code == 202
            run_id = response.json()["run_id"]
            for _ in range(500):
                record = (await http.get(f"/api/v1/experiments/{run_id}")).json()
                if record["status"] != "RUNNING":
                    break
                await asyncio.sleep(0.02)
            assert record["status"] == "FINISHED"
            exported = await http.get(f"/api/v1/experiments/{run_id}/export")
            assert exported.json()["result"]["source"] == "discrete-event-simulation"
            assert "attachment" in exported.headers["content-disposition"]
            real = await http.get("/api/v1/observations/export")
            assert real.json()["source"] == "real-client-snapshot"
            assert real.json()["tasks"] == []
