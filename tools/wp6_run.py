"""Submit a fixed mixed workload to a running local TaskLoom client.

Example: python tools/wp6_run.py --base-url http://127.0.0.1:8795 \
    --scenario unconstrained --output run.json

Start the client and platform with capacities matching the selected scenario first.
This runner checks observed capacities and labels four contention conditions; it does
not implement or claim any B00/B10/B01/B11/B11+FB scheduling strategy.
"Short/long" is deadline urgency; "light/heavy" is local tool hash work.
The default four repeats follow a seeded order at 0.05-second planned intervals.
Observed submission offsets are recorded separately; pending polling timeouts remain
incomplete with unknown quality.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

HEADERS = {"X-TaskLoom-Local": "1"}
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "UNKNOWN"}


def plan_workload(
    config: dict[str, Any], *, seed: int, repeats: int, interarrival_seconds: float
) -> list[dict[str, Any]]:
    """Return a repeatable order and relative arrival schedule."""
    if not 1 <= repeats <= 100 or not 0 <= interarrival_seconds <= 60:
        raise ValueError("repeats or interarrival_seconds outside bounds")
    workload = config.get("workload")
    if not isinstance(workload, list) or not workload:
        raise ValueError("workload must contain task classes")
    rng = random.Random(seed)
    ordered = [dict(item) for _ in range(repeats) for item in workload]
    rng.shuffle(ordered)
    return [
        {**item, "planned_submission_offset_seconds": index * interarrival_seconds}
        for index, item in enumerate(ordered)
    ]


def run_workload(
    client: httpx.Client,
    config: dict[str, Any],
    scenario: str,
    *,
    seed: int,
    timeout_seconds: float = 180,
    repeats: int = 4,
    interarrival_seconds: float = 0.05,
    checkpoint: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Submit all task classes, then collect terminal outcomes and validate results."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    try:
        expected = config["scenarios"][scenario]
    except KeyError as error:
        raise ValueError(f"unknown scenario: {scenario}") from error
    plan = plan_workload(
        config, seed=seed, repeats=repeats, interarrival_seconds=interarrival_seconds
    )
    resources = client.get("/api/v1/resources")
    resources.raise_for_status()
    platform = client.get("/api/v1/platform")
    platform.raise_for_status()
    feedback = platform.json().get("feedback")
    if not platform.json().get("connected") or not feedback:
        raise ValueError("platform is not connected")
    resource_state = resources.json()
    observed = {
        "tool_slots": resource_state["capacity"]["slots"],
        "pool_capacity": feedback["pool_capacity"],
        "account_max_inflight": feedback.get("account_max_inflight"),
        "local_model_slots": resource_state.get("model_capacity", {}).get("slots"),
    }
    if any(observed[key] != value for key, value in expected.items()):
        raise ValueError(f"capacity mismatch: expected {expected}, observed {observed}")
    if (
        not isinstance(observed["account_max_inflight"], int)
        or not isinstance(observed["local_model_slots"], int)
        or observed["account_max_inflight"] < expected["pool_capacity"]
        or observed["local_model_slots"] < expected["pool_capacity"]
    ):
        raise ValueError(f"hidden API capacity limit: {observed}")

    run_id = uuid.uuid4().hex
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(plan):
        if item["deadline_seconds"] <= 0 or item["work_iterations"] <= 0:
            raise ValueError("deadline_seconds and work_iterations must be positive")
        name = item["class"]
        document = f"TaskLoom {name} seed {seed}"
        rows.append(
            {
                "observation_id": f"{run_id}-{index}",
                "class": name,
                "submission_state": "PLANNED",
                "quality_passed": None,
                "request": {
                    "title": f"WP6 {name} {run_id}-{index}",
                    "instruction": "请核对文件数量和词数。",
                    "documents": {"notes.txt": document},
                    "deadline_seconds": item["deadline_seconds"],
                    "work_iterations": item["work_iterations"],
                },
                "expected_result": {"files": 1, "words": len(document.split())},
                "planned_submission_offset_seconds": item["planned_submission_offset_seconds"],
            }
        )
    manifest = {
        "source": "observed-local-api",
        "run_id": run_id,
        "scenario": scenario,
        "seed": seed,
        "repeats": repeats,
        "interarrival_seconds": interarrival_seconds,
        "conditions": observed,
        "incomplete": True,
        "platform": {
            "pool_kind": feedback.get("pool_kind"),
            "model": feedback.get("model"),
            "account_max_inflight": feedback.get("account_max_inflight"),
        },
        "policy_claim": "none; capacity labels only",
        "tasks": rows,
    }

    def save() -> None:
        manifest["incomplete"] = any(
            row["submission_state"] not in {"ACCEPTED", "REJECTED"}
            or (row["submission_state"] == "ACCEPTED" and row.get("quality_passed") is None)
            for row in rows
        )
        if checkpoint is not None:
            checkpoint(manifest)

    started = time.monotonic()
    save()
    try:
        for row in rows:
            remaining = row["planned_submission_offset_seconds"] - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)
            row["actual_submission_offset_seconds"] = time.monotonic() - started
            # Durable intent precedes POST: an interrupted/ambiguous send is never replayed.
            row["submission_state"] = "SUBMITTING"
            save()
            try:
                response = client.post("/api/v1/tasks", headers=HEADERS, json=row["request"])
                row["http_status"] = response.status_code
                if 400 <= response.status_code < 500 and response.status_code != 408:
                    row.update(
                        submission_state="REJECTED",
                        outcome="REJECTED",
                        quality_passed=False,
                        error=f"HTTP_{response.status_code}",
                    )
                else:
                    response.raise_for_status()
                    task_id = response.json().get("task_id")
                    if not isinstance(task_id, str) or not task_id:
                        raise ValueError("Submission response is missing task_id")
                    row.update(task_id=task_id, submission_state="ACCEPTED")
            except (httpx.HTTPError, ValueError, TypeError) as error:
                row.update(submission_state="UNKNOWN", error=type(error).__name__)
            save()

        deadline = time.monotonic() + timeout_seconds
        for row in rows:
            if row["submission_state"] != "ACCEPTED":
                continue
            while True:
                try:
                    response = client.get(f"/api/v1/tasks/{row['task_id']}")
                    response.raise_for_status()
                    task = response.json()
                    if not isinstance(task, dict):
                        raise ValueError("Task response is malformed")
                except (httpx.HTTPError, ValueError) as error:
                    row["poll_error"] = type(error).__name__
                    break
                if task["outcome"] in TERMINAL:
                    result = task.get("result") or {}
                    row.update(
                        outcome=task["outcome"],
                        quality_passed=None
                        if task["outcome"] == "UNKNOWN"
                        else (
                            task["outcome"] == "SUCCEEDED"
                            and all(
                                result.get(key) == value
                                for key, value in row["expected_result"].items()
                            )
                        ),
                        actual_result=result,
                        created_at=task.get("created_at"),
                        completed_at=task.get("completed_at"),
                        deadline_at=task.get("deadline_at"),
                    )
                    break
                if time.monotonic() >= deadline:
                    row.update(outcome=task["outcome"], quality_passed=None, timed_out_polling=True)
                    break
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            save()
    finally:
        save()
    return manifest


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    """Atomically replace the experiment checkpoint after every state transition."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Running local TaskLoom client URL")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "configs/experiments/workload.json",
    )
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=4)
    parser.add_argument("--interarrival-seconds", type=float, default=0.05)
    parser.add_argument("--timeout-seconds", type=float, default=180)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    with httpx.Client(base_url=args.base_url, timeout=10, trust_env=False) as client:
        result = run_workload(
            client,
            config,
            args.scenario,
            seed=args.seed,
            timeout_seconds=args.timeout_seconds,
            repeats=args.repeats,
            interarrival_seconds=args.interarrival_seconds,
            checkpoint=lambda manifest: write_manifest(args.output, manifest),
        )
    write_manifest(args.output, result)


if __name__ == "__main__":
    main()
