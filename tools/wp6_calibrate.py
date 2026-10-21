"""Derive measured WP6 simulation inputs from a client snapshot and run manifest.

Example: python tools/wp6_calibrate.py --snapshot capture.json \
    --manifest run.json --output calibration.json

Attempt running→finished intervals exclude the preceding local queue wait. A model
interval can still contain platform L4 wait; the output records this limitation.
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
        costs.append(cost)

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in events:
        if not isinstance(row, dict):
            raise ValueError("event row is malformed")
        if row.get("root_task_id") not in selected_ids:
            continue
        event_type = row.get("type")
        attempt_id = row.get("attempt_id")
        if not isinstance(event_type, str) or not isinstance(attempt_id, str):
            continue  # Root task events are not attempt intervals.
        kind, separator, state = event_type.partition(".")
        if not separator or kind not in {"tool", "model"}:
            continue
        grouped[(kind, attempt_id)].append(row)

    durations: dict[str, list[float]] = {"tool": [], "model": []}
    excluded_attempts = {"incomplete_or_unknown": 0, "malformed_interval": 0}
    for (kind, attempt_id), rows in grouped.items():
        starts = [row for row in rows if row["type"] == f"{kind}.running"]
        finishes = [row for row in rows if row["type"] == f"{kind}.finished"]
        if not finishes:
            excluded_attempts["incomplete_or_unknown"] += 1
            continue  # Incomplete or unknown attempts are not measured executions.
        if len(finishes) != 1:
            raise ValueError(f"ambiguous finish events for {attempt_id}")
        if finishes[0].get("outcome") != "SUCCEEDED":
            excluded_attempts["incomplete_or_unknown"] += 1
            continue
        if not starts:
            excluded_attempts["malformed_interval"] += 1
            continue
        try:
            start_times = [_time(row.get("occurred_at")) for row in starts]
            finish_time = _time(finishes[0].get("occurred_at"))
        except ValueError:
            excluded_attempts["malformed_interval"] += 1
            continue
        if any(at > finish_time for at in start_times):
            excluded_attempts["malformed_interval"] += 1
            continue
        duration = (finish_time - min(start_times)).total_seconds()
        if duration <= 0:
            excluded_attempts["malformed_interval"] += 1
            continue
        durations[kind].append(duration)
    if not durations["tool"] or not durations["model"] or not costs:
        raise ValueError("missing tool, model, or settled cost samples")
    return {
        "provenance": "measured",
        "source": source,
        "manifest_source": manifest_source,
        "measurement_scope": {
            "tool_seconds": "client attempt running to finished; local queue excluded",
            "api_seconds": "client model attempt running to finished; platform wait may be included",
            "cost_units": "terminal root task consumed_units after reservation settlement",
        },
        "tool_seconds": durations["tool"],
        "api_seconds": durations["model"],
        "cost_units": costs,
        "sample_counts": {
            "tool": len(durations["tool"]),
            "api": len(durations["model"]),
            "cost": len(costs),
        },
        "excluded_attempts": excluded_attempts,
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
