"""Single-flight comparison execution with a bounded, durable result ring."""

import asyncio
import time
from typing import Any

from taskloom.domain.identity import identity, utc_now
from taskloom.domain.model import DomainError
from taskloom.domain.ports import Repository, StoredRecord
from taskloom.experiments.comparison import ComparisonSpec, compare


class ComparisonService:
    history_limit = 12

    def __init__(self, store: Repository, tenant: str) -> None:
        self.store, self.tenant = store, tenant
        self.job: asyncio.Task[None] | None = None
        self.lock = asyncio.Lock()
        self.sequence = 0
        self.slots: dict[str, str] = {}

    async def start(self) -> None:
        rows = await self.store.records("comparison", self.tenant)
        for row in rows:
            self.sequence = max(self.sequence, row["sequence"])
            self.slots[row["run_id"]] = row["slot"]
            if row["status"] == "RUNNING":
                row.update(
                    status="FAILED",
                    error="进程重启中断了计算，请重新运行。",
                    completed_at=utc_now(),
                )
                await self._save(row)

    async def _save(self, record: dict[str, Any]) -> None:
        await self.store.apply([StoredRecord("comparison", record["slot"], self.tenant, record)])

    async def submit(self, spec: ComparisonSpec) -> dict[str, Any]:
        async with self.lock:
            if self.job is not None and not self.job.done():
                raise DomainError("EXPERIMENT_CONFLICT", "已有对照实验正在计算，请等待完成。")
            self.sequence += 1
            slot = str(self.sequence % self.history_limit)
            self.slots = {key: value for key, value in self.slots.items() if value != slot}
            record: dict[str, Any] = {
                "run_id": identity(),
                "slot": slot,
                "sequence": self.sequence,
                "status": "RUNNING",
                "created_at": utc_now(),
                "completed_at": None,
                "spec": spec.model_dump(),
                "result": None,
                "error": None,
            }
            await self._save(record)
            self.slots[record["run_id"]] = slot
            self.job = asyncio.create_task(self._produce(record, spec), name="comparison")
            return dict(record)

    async def _produce(self, record: dict[str, Any], spec: ComparisonSpec) -> None:
        # The bounded CPU calculation runs outside the event loop and repository transaction.
        try:
            started = time.monotonic()
            result = await asyncio.to_thread(compare, spec)
            record.update(
                status="FINISHED", result=result, calculation_seconds=time.monotonic() - started
            )
        except Exception as error:
            record.update(status="FAILED", error="仿真计算失败：" + type(error).__name__)
        record["completed_at"] = utc_now()
        await self._save(record)

    async def history(self) -> list[dict[str, Any]]:
        rows = await self.store.records("comparison", self.tenant)
        return [
            {key: value for key, value in row.items() if key != "result"}
            for row in sorted(rows, key=lambda row: row["sequence"], reverse=True)
        ]

    async def get(self, run_id: str) -> dict[str, Any]:
        slot = self.slots.get(run_id)
        record = await self.store.get("comparison", slot) if slot is not None else None
        if record is None or record["run_id"] != run_id:
            raise DomainError("NOT_FOUND", "实验记录不存在或已被历史容量策略回收。")
        return record

    async def close(self) -> None:
        # A Python thread cannot be forcibly cancelled safely; finish its bounded work before
        # the repository closes, rather than publishing a partial result as completed.
        if self.job is not None:
            await asyncio.gather(self.job, return_exceptions=True)
