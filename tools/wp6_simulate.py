"""Reproducible, explicitly simulated two-stage WP6 workload.

No TaskLoom scheduling algorithm is reproduced here. Each stage uses FIFO slots.
Calibration distributions are supplied by a caller and retain their provenance.
"""

from __future__ import annotations

import argparse
import heapq
import json
import random
from pathlib import Path
from typing import Any

DAY_SECONDS = 86400
SCENARIOS = {
    "unconstrained": (64, 64),
    "tool_constrained": (1, 64),
    "api_constrained": (64, 1),
    "both_constrained": (1, 1),
}


def _calibration(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("provenance") not in {"synthetic-fixture", "measured"}:
        raise ValueError("calibration provenance must be synthetic-fixture or measured")
    for key in ("tool_seconds", "api_seconds", "cost_units"):
        values = data.get(key)
        if (
            not isinstance(values, list)
            or not values
            or any(
                isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0
                for value in values
            )
        ):
            raise ValueError(f"{key} must be a non-empty list of positive numbers")
    if data["provenance"] == "measured" and not data.get("source"):
        raise ValueError("measured calibration requires a source")
    return data


def _stage(ready: float, duration: float, slots: list[float]) -> tuple[float, float]:
    available = heapq.heappop(slots)
    start = max(ready, available)
    finished = start + duration
    heapq.heappush(slots, finished)
    return start, finished


def run_experiment(
    calibration_path: Path, *, seed: int, days: int = 30, arrivals_per_day: int = 24
) -> dict[str, Any]:
    """Run four capacity conditions over identical seeded arrivals and work.

    Results are discrete-event simulation outputs, never observed runtime metrics.
    """
    if days < 1 or arrivals_per_day < 1 or days > 365 or arrivals_per_day > 10000:
        raise ValueError("days and arrivals_per_day must be positive and bounded")
    calibration = _calibration(calibration_path)
    rng = random.Random(seed)
    workload = sorted(
        (
            day * DAY_SECONDS + rng.random() * DAY_SECONDS,
            day * arrivals_per_day + ordinal,
            rng.choice(calibration["tool_seconds"]),
            rng.choice(calibration["api_seconds"]),
            rng.choice(calibration["cost_units"]),
        )
        for day in range(days)
        for ordinal in range(arrivals_per_day)
    )
    events: list[dict[str, Any]] = []
    summary: list[dict[str, Any]] = []
    for scenario, (tool_capacity, api_capacity) in SCENARIOS.items():
        tool_slots = [0.0] * tool_capacity
        api_slots = [0.0] * api_capacity
        tool_waits: list[float] = []
        api_waits: list[float] = []
        completed: list[float] = []
        total_cost = 0.0
        tool_finishes: list[tuple[float, int, float]] = []
        for arrival, task_id, tool_duration, api_duration, cost in workload:
            tool_start, tool_end = _stage(arrival, tool_duration, tool_slots)
            tool_waits.append(tool_start - arrival)
            tool_finishes.append((tool_end, task_id, api_duration))
            total_cost += cost
            common = {"source": "simulation", "scenario": scenario, "task_id": task_id}
            events.extend(
                [
                    {**common, "type": "arrived", "at_seconds": arrival},
                    {**common, "type": "tool_completed", "at_seconds": tool_end},
                ]
            )
        for tool_end, task_id, api_duration in sorted(tool_finishes):
            api_start, api_end = _stage(tool_end, api_duration, api_slots)
            api_waits.append(api_start - tool_end)
            completed.append(api_end)
            events.append(
                {
                    "source": "simulation",
                    "scenario": scenario,
                    "task_id": task_id,
                    "type": "task_completed",
                    "at_seconds": api_end,
                }
            )
        count = len(workload)
        summary.append(
            {
                "source": "simulation",
                "scenario": scenario,
                "tool_capacity": tool_capacity,
                "api_capacity": api_capacity,
                "submitted_tasks": count,
                "completed_tasks": count,
                "mean_tool_wait_seconds": sum(tool_waits) / count,
                "mean_api_wait_seconds": sum(api_waits) / count,
                "completed_at_seconds": max(completed),
                "total_cost_units": total_cost,
            }
        )
    return {
        "metadata": {
            "source": "simulation",
            "calibration_provenance": calibration["provenance"],
            "calibration_source": calibration.get("source"),
            "policy": "FIFO per stage; no B00/B10/B01/B11/B11+FB claims",
            "seed": seed,
            "days": days,
            "arrivals_per_day": arrivals_per_day,
        },
        "events": events,
        "summary": summary,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--arrivals-per-day", type=int, default=24)
    args = parser.parse_args()
    result = run_experiment(
        args.calibration,
        seed=args.seed,
        days=args.days,
        arrivals_per_day=args.arrivals_per_day,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
