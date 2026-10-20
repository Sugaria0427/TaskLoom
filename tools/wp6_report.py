"""Summarize a JSON TaskState export and optional quality verdicts.

Usage: python tools/wp6_report.py tasks.json --quality quality.json --json report.json --csv tasks.csv
The task input is a JSON list or {"tasks": [...]} envelope. Quality is {task_id: boolean}.
"""

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

from taskloom.experiments.metrics import summarize_tasks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tasks", type=Path, help="TaskState JSON export")
    parser.add_argument("--quality", type=Path, help="JSON map of task ID to pass/fail")
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
    report = summarize_tasks(records, quality=quality, source=str(args.tasks))
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
