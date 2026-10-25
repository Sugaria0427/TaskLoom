"""Derive measured WP6 simulation inputs from a client snapshot and run manifest.

Example: python tools/wp6_calibrate.py --snapshot capture.json \
    --manifest run.json --output calibration.json

Measured roots preserve every ordered service stage and its settled model cost.
Model service comes from L4 dispatch/completion timestamps; L4 wait is kept as an
observation and never reused as service time.
Only successful, quality-checked root tasks with settled positive consumption
contribute costs. Missing or ambiguous samples fail without filling values.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("event occurred_at is missing")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("event occurred_at is malformed") from error
    if result.tzinfo is None:
        raise ValueError("event occurred_at must include a timezone")
    return result


def calibrate(
    snapshot: dict[str, Any],
    manifest: dict[str, Any],
    *,
    source: str,
    manifest_source: str,
) -> dict[str, Any]:
    """Extract positive observed durations and settled cost samples."""
    if not source or not manifest_source:
        raise ValueError("snapshot and manifest source paths are required")
    if snapshot.get("source", {}).get("kind") != "real-client-snapshot":
        raise ValueError("snapshot must come from a real client")
    if manifest.get("incomplete"):
        raise ValueError("run manifest is incomplete")
    manifest_tasks = manifest.get("tasks")
    snapshot_tasks = snapshot.get("tasks")
    events = snapshot.get("events")
    if (
        not isinstance(manifest_tasks, list)
        or not isinstance(snapshot_tasks, list)
        or not isinstance(events, list)
    ):
        raise ValueError("snapshot or manifest collections are malformed")
    selected_ids: set[str] = set()
    seen_ids: set[str] = set()
    for row in manifest_tasks:
        if not isinstance(row, dict):
            raise ValueError("run manifest task row is malformed")
        if row.get("submission_state") == "REJECTED":
            continue
        task_id = row.get("task_id")
        if not isinstance(task_id, str) or task_id in seen_ids:
            raise ValueError("run manifest task IDs are missing or duplicated")
        seen_ids.add(task_id)
        if row.get("outcome") == "PENDING" or row.get("quality_passed") is None:
            raise ValueError("run manifest contains incomplete task observations")
        if row.get("outcome") == "SUCCEEDED" and row.get("quality_passed") is True:
            selected_ids.add(task_id)
    if not selected_ids:
        raise ValueError("no successful quality-checked tasks")
    by_id = {row.get("task_id"): row for row in snapshot_tasks}
    if len(by_id) != len(snapshot_tasks):
        raise ValueError("snapshot task IDs are duplicated")
    attempts = snapshot.get("resources", {}).get("attempts", [])
    by_attempt = {row["attempt_id"]: row for row in attempts}
    if len(by_attempt) != len(attempts):
        raise ValueError("snapshot attempt IDs are duplicated")
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in events:
        if not isinstance(row, dict):
            raise ValueError("event row is malformed")
        if row.get("root_task_id") not in selected_ids:
            continue
        kind, _, state = str(row.get("type", "")).partition(".")
        attempt_id = row.get("attempt_id")
        if kind in {"model", "tool"} and isinstance(attempt_id, str):
            grouped[(row["root_task_id"], attempt_id)].append(row)

    workflows: list[dict[str, Any]] = []
    durations: dict[str, list[float]] = {"tool": [], "model": []}
    costs: list[int | float] = []
    for task_id in sorted(selected_ids):
        task = by_id.get(task_id)
        if task is None or task.get("outcome") != "SUCCEEDED":
            raise ValueError(f"selected task {task_id} is missing or not terminal success")
        cost = task.get("consumed_units")
        if (
            task.get("quiescent") is not True
            or task.get("reserved_units") != 0
            or isinstance(cost, bool)
            or not isinstance(cost, (int, float))
            or cost <= 0
        ):
            raise ValueError(f"selected task {task_id} has unsettled or invalid cost")
        stages: list[dict[str, Any]] = []
        for (root, attempt_id), rows in grouped.items():
            if root != task_id:
                continue
            kind = rows[0]["type"].partition(".")[0]
            starts = [row for row in rows if row["type"] == f"{kind}.running"]
            finishes = [row for row in rows if row["type"] == f"{kind}.finished"]
            if len(finishes) != 1 or not starts:
                raise ValueError(f"incomplete or ambiguous interval for {attempt_id}")
            if finishes[0].get("outcome") != "SUCCEEDED":
                raise ValueError(f"unsupported failed execution trace for {attempt_id}")
            start = min(_time(row.get("occurred_at")) for row in starts)
            finish = _time(finishes[0].get("occurred_at"))
            if any(_time(row.get("occurred_at")) >= finish for row in starts):
                raise ValueError(f"malformed interval for {attempt_id}")
            seconds = (finish - start).total_seconds()
            units: int | float = 0
            queue_seconds = 0.0
            if kind == "model":
                saved = by_attempt.get(attempt_id, {})
                if saved.get("root_task_id") != task_id:
                    raise ValueError(f"model timing unavailable for {attempt_id}")
                timing = saved.get("execution_ref") or {}
                created = _time(timing.get("platform_created_at"))
                dispatched = _time(timing.get("platform_dispatched_at"))
                completed = _time(timing.get("platform_completed_at"))
                seconds = (completed - dispatched).total_seconds()
                queue_seconds = (dispatched - created).total_seconds()
                units = saved.get("consumed_units")
                if (
                    seconds <= 0
                    or queue_seconds < 0
                    or isinstance(units, bool)
                    or not isinstance(units, (int, float))
                    or units < 0
                    or not saved.get("settled")
                ):
                    raise ValueError(f"unsettled or malformed model timing for {attempt_id}")
            if seconds <= 0:
                raise ValueError(f"malformed interval for {attempt_id}")
            stages.append(
                {
                    "attempt_id": attempt_id,
                    "kind": kind,
                    "seconds": seconds,
                    "cost_units": units,
                    "observed_platform_wait_seconds": queue_seconds,
                    "observed_start": start.isoformat(),
                    "observed_end": finish.isoformat(),
                }
            )
        stages.sort(key=lambda stage: _time(stage["observed_start"]))
        if not stages or not any(stage["kind"] == "model" for stage in stages):
            raise ValueError(f"missing workflow stages for {task_id}")
        if any(
            _time(left["observed_end"]) > _time(right["observed_start"])
            for left, right in zip(stages, stages[1:], strict=False)
        ):
            raise ValueError("overlapping stages require a dependency-graph simulator")
        if sum(stage["cost_units"] for stage in stages) != cost:
            raise ValueError(f"stage costs do not reconcile with root {task_id}")
        for stage in stages:
            durations[stage["kind"]].append(stage["seconds"])
        costs.append(cost)
        workflows.append({"source_task_id": task_id, "stages": stages, "cost_units": cost})
    if not durations["tool"] or not durations["model"]:
        raise ValueError("missing tool or model service samples")
    return {
        "schema_version": 2,
        "provenance": "measured",
        "source": source,
        "manifest_source": manifest_source,
        "measurement_scope": {
            "tool_seconds": "client tool running-to-finished interval, excluding L1 wait",
            "api_seconds": "L4 dispatched-to-completed service; L4 queue excluded",
            "workflow": "ordered sequential service stages; observed queue waits and gaps are not replayed",
            "cost_units": "per-model settled charges reconcile with terminal root consumption",
        },
        "workflows": workflows,
        "tool_seconds": durations["tool"],
        "api_seconds": durations["model"],
        "cost_units": costs,
        "sample_counts": {
            "tool": len(durations["tool"]),
            "api": len(durations["model"]),
            "cost": len(costs),
        },
        "excluded_attempts": {"incomplete_or_unknown": 0, "malformed_interval": 0},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    result = calibrate(
        snapshot,
        manifest,
        source=str(args.snapshot.resolve()),
        manifest_source=str(args.manifest.resolve()),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
