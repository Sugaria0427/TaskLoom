"""Summarize a JSON TaskState export and optional quality verdicts.

Usage: python tools/wp6_report.py tasks.json --run-manifest run.json --json report.json --csv tasks.csv
The task input is a JSON list or {"tasks": [...]} envelope. A run manifest selects
only its task IDs and supplies quality verdicts. Alternatively, --quality accepts
{task_id: boolean} for an export that already contains only the intended cohort.
"""

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

from taskloom.experiments.metrics import summarize_tasks


def _select_run(records: list[Any], manifest: Any) -> tuple[list[Any], dict[str, bool]]:
    if (
        not isinstance(manifest, dict)
        or manifest.get("source") != "observed-local-api"
        or not isinstance(manifest.get("tasks"), list)
        or not manifest["tasks"]
    ):
        raise ValueError("run manifest must contain observed-local-api tasks")
    verdicts: dict[str, bool] = {}
    for row in manifest["tasks"]:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("task_id"), str)
            or not row["task_id"]
        ):
            raise ValueError("run manifest has an invalid task_id")
        identity = row["task_id"]
        if identity in verdicts:
            raise ValueError(f"duplicate task_id in run manifest: {identity}")
        if row.get("incomplete") is True or row.get("quality_passed") is None:
            raise ValueError(
                f"task_id {identity} is incomplete or quality_passed is unavailable; "
                "resume the run or recapture after completion"
            )
        if type(row.get("quality_passed")) is not bool:
            raise ValueError(f"quality_passed must be boolean for task_id {identity}")
        verdicts[identity] = row["quality_passed"]
    selected: dict[str, Any] = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("task records must be JSON objects")
        identity = record.get("task_id")
        if isinstance(identity, str) and identity in verdicts:
            if identity in selected:
                raise ValueError(f"duplicate task_id in captured snapshot: {identity}")
            selected[identity] = record
    missing = set(verdicts) - set(selected)
    if missing:
        raise ValueError(f"missing task_id in captured snapshot: {sorted(missing)[0]}")
    return [selected[identity] for identity in verdicts], verdicts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tasks", type=Path, help="TaskState JSON export")
    cohort = parser.add_mutually_exclusive_group()
    cohort.add_argument("--quality", type=Path, help="JSON map of task ID to pass/fail")
    cohort.add_argument(
        "--run-manifest", type=Path, help="wp6_run.py result; selects this run's tasks"
    )
    parser.add_argument("--json", type=Path, dest="json_output", help="summary output path")
    parser.add_argument("--csv", type=Path, dest="csv_output", help="per-task CSV output path")
    args = parser.parse_args()
    raw: Any = json.loads(args.tasks.read_text(encoding="utf-8"))
    records = raw["tasks"] if isinstance(raw, dict) else raw
    if not isinstance(records, list):
        parser.error("task export must be a JSON list or an object with a tasks list")
    quality: Any = None
    if args.quality:
        quality = json.loads(args.quality.read_text(encoding="utf-8"))
        if not isinstance(quality, dict) or any(
            type(value) is not bool for value in quality.values()
        ):
            parser.error("quality file must map task IDs to JSON booleans")
    if args.run_manifest:
        manifest = json.loads(args.run_manifest.read_text(encoding="utf-8"))
        try:
            records, quality = _select_run(records, manifest)
        except ValueError as error:
            parser.error(str(error))
    report = summarize_tasks(records, quality=quality, source=str(args.tasks))
    if args.run_manifest:
        report["provenance"]["run_manifest"] = str(args.run_manifest)
        report["provenance"]["scenario"] = manifest.get("scenario")
        report["provenance"]["seed"] = manifest.get("seed")
    serialized = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.json_output:
        args.json_output.write_text(serialized, encoding="utf-8")
    else:
        sys.stdout.write(serialized)
    if args.csv_output:
        fields = [
            "task_id",
            "tenant_id",
            "outcome",
            "duration_seconds",
            "on_time",
            "quality_passed",
            "on_time_quality_success",
            "timed_out",
            "consumed_units",
            "reserved_units",
        ]
        with args.csv_output.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(report["tasks"])


if __name__ == "__main__":
    main()
