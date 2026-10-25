"""Reproducible FIFO capacity simulation using ordered measured workflows or a synthetic fixture.

No TaskLoom scheduling algorithm is reproduced here. Each stage uses FIFO slots.
Calibration distributions are supplied by a caller and retain their provenance.
"""

from __future__ import annotations

import argparse
import heapq
import json
import math
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
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
                for value in values
            )
        ):
            raise ValueError(f"{key} must be a non-empty list of positive numbers")
    if data["provenance"] == "measured":
        if not data.get("source"):
            raise ValueError("measured calibration requires a source")
        workflows = data.get("workflows")
        if not isinstance(workflows, list) or not workflows:
            raise ValueError("measured calibration requires complete ordered workflows")
        for workflow in workflows:
            stages = workflow.get("stages")
            if not isinstance(stages, list) or not stages:
                raise ValueError("measured workflow stages are missing")
            for stage in stages:
                if stage.get("kind") not in {"model", "tool"}:
                    raise ValueError("unknown workflow stage kind")
                for key in ("seconds", "cost_units"):
                    value = stage.get(key)
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(value)
                        or value < 0
                        or (key == "seconds" and value == 0)
                    ):
                        raise ValueError("invalid workflow duration or cost")
            if sum(stage["cost_units"] for stage in stages) != workflow.get("cost_units"):
                raise ValueError("workflow stage cost differs from root cost")
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
    measured = calibration["provenance"] == "measured"
    workload: list[tuple[float, int, list[dict[str, Any]], float]] = []
    for day in range(days):
        for ordinal in range(arrivals_per_day):
            arrival = day * DAY_SECONDS + rng.random() * DAY_SECONDS
            if measured:
                sample = rng.choice(calibration["workflows"])
                stages, cost = sample["stages"], sample["cost_units"]
            else:
                stages = [
                    {"kind": "tool", "seconds": rng.choice(calibration["tool_seconds"])},
                    {"kind": "model", "seconds": rng.choice(calibration["api_seconds"])},
                ]
                cost = rng.choice(calibration["cost_units"])
            workload.append((arrival, day * arrivals_per_day + ordinal, stages, cost))
    workload.sort()
    events: list[dict[str, Any]] = []
    summary: list[dict[str, Any]] = []
    for scenario, (tool_capacity, api_capacity) in SCENARIOS.items():
        slots = {"tool": [0.0] * tool_capacity, "model": [0.0] * api_capacity}
        waits = {"tool": 0.0, "model": 0.0}
        stage_counts = {"tool": 0, "model": 0}
        completed: list[float] = []
        by_task = {task_id: stages for _, task_id, stages, _ in workload}
        # Pop all newly ready stages chronologically, including later model rounds.
        ready = [
            (arrival, index, task_id, 0) for index, (arrival, task_id, _, _) in enumerate(workload)
        ]
        heapq.heapify(ready)
        sequence = len(ready)
        for arrival, task_id, _, _ in workload:
            events.append(
                {
                    "source": "simulation",
                    "scenario": scenario,
                    "task_id": task_id,
                    "type": "arrived",
                    "at_seconds": arrival,
                }
            )
        while ready:
            at, _, task_id, index = heapq.heappop(ready)
            stages = by_task[task_id]
            stage = stages[index]
            kind = stage["kind"]
            start, end = _stage(at, stage["seconds"], slots[kind])
            waits[kind] += start - at
            stage_counts[kind] += 1
            if measured or kind == "tool":
                events.append(
                    {
                        "source": "simulation",
                        "scenario": scenario,
                        "task_id": task_id,
                        "stage_index": index,
                        "type": f"{kind}_completed",
                        "at_seconds": end,
                        "started_at_seconds": start,
                        "ready_at_seconds": at,
                    }
                )
            if index + 1 < len(stages):
                heapq.heappush(ready, (end, sequence, task_id, index + 1))
                sequence += 1
            else:
                completed.append(end)
                events.append(
                    {
                        "source": "simulation",
                        "scenario": scenario,
                        "task_id": task_id,
                        "type": "task_completed",
                        "at_seconds": end,
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
                "model_stages": stage_counts["model"],
                "tool_stages": stage_counts["tool"],
                "mean_tool_wait_seconds": waits["tool"] / count,
                "mean_api_wait_seconds": waits["model"] / count,
                "completed_at_seconds": max(completed),
                "total_cost_units": sum(cost for _, _, _, cost in workload),
            }
        )
    events.sort(key=lambda event: (event["scenario"], event["at_seconds"]))
    return {
        "metadata": {
            "source": "simulation",
            "calibration_provenance": calibration["provenance"],
            "calibration_source": calibration.get("source"),
            "workflow_model": "ordered measured services"
            if measured
            else "synthetic tool-then-model fixture",
            "queue_handling": "all waits generated by this simulation; observed platform waits excluded",
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
