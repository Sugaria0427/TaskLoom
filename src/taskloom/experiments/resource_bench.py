"""Paired finite arrivals through the production tool dispatcher; no model calls."""

import asyncio
import math
import time

from taskloom.client.dispatcher import Dispatcher
from taskloom.contracts.models import JsonObject, digest
from taskloom.domain.model import Resources


async def protection_trial(enabled: bool) -> JsonObject:
    workload = [("large", 0.005, 6, 0.012)] + [
        (f"light-{i}", i * 0.002, 2, 0.008) for i in range(80)
    ]
    dispatcher = Dispatcher(
        Resources(slots=3, memory_bytes=6),
        max_pending=128,
        protect_large=enabled,
        protection_seconds=0.02,
        bypass_limit=8,
    )
    await dispatcher.start()
    start = time.monotonic()
    cpu_start = time.process_time()
    waits: dict[str, float] = {}
    peak = 0

    async def submit(key: str, arrival: float, memory: int, duration: float) -> None:
        await asyncio.sleep(max(0, start + arrival - time.monotonic()))
        queued = time.monotonic()

        async def execute() -> None:
            nonlocal peak
            waits[key] = time.monotonic() - queued
            peak = max(
                peak, sum(value.memory_bytes for value in dispatcher.pool.allocations.values())
            )
            await asyncio.sleep(duration)

        await dispatcher.submit(
            key,
            key,
            Resources(slots=1, memory_bytes=memory),
            execute,
            priority="low" if key == "large" else "high",
        )

    try:
        async with asyncio.timeout(10):
            await asyncio.gather(*(submit(*row) for row in workload))
        elapsed = time.monotonic() - start
        light = sorted(wait for key, wait in waits.items() if key != "large")
        return {
            "source": "controlled-production-dispatcher",
            "protection_enabled": enabled,
            "workload_hash": digest(workload),
            "submitted": len(workload),
            "completed": len(waits),
            "large_wait_seconds": waits["large"],
            "light_p95_wait_seconds": light[math.ceil(0.95 * len(light)) - 1],
            "peak_memory_units": peak,
            "capacity_memory_units": 6,
            "wall_seconds": elapsed,
            "harness_cpu_seconds": time.process_time() - cpu_start,
            "tasks_per_second": len(waits) / elapsed,
            "allocations_remaining": len(dispatcher.pool.allocations),
        }
    finally:
        await dispatcher.close()
