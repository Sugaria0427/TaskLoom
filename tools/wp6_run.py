"""Submit a fixed mixed workload to a running local TaskLoom client.

Example: python tools/wp6_run.py --base-url http://127.0.0.1:8795 \
    --scenario unconstrained --output run.json

Start the client and platform with capacities matching the selected scenario first.
This runner checks observed capacities and labels four contention conditions; it does
not implement or claim any B00/B10/B01/B11/B11+FB scheduling strategy.
"Short/long" is deadline urgency; "light/heavy" is local tool hash work.
"""

from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path
from typing import Any

import httpx

HEADERS = {"X-TaskLoom-Local": "1"}
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "UNKNOWN"}


def run_workload(
    client: httpx.Client,
    config: dict[str, Any],
    scenario: str,
    *,
    seed: int,
    timeout_seconds: float = 180,
) -> dict[str, Any]:
    """Submit all task classes, then collect terminal outcomes and validate results."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    try:
        expected = config["scenarios"][scenario]
    except KeyError as error:
        raise ValueError(f"unknown scenario: {scenario}") from error
    if not config.get("workload"):
        raise ValueError("workload must contain task classes")
    resources = client.get("/api/v1/resources")
    resources.raise_for_status()
    platform = client.get("/api/v1/platform")
    platform.raise_for_status()
    feedback = platform.json().get("feedback")
    if not platform.json().get("connected") or not feedback:
        raise ValueError("platform is not connected")
    observed = {
        "tool_slots": resources.json()["capacity"]["slots"],
        "pool_capacity": feedback["pool_capacity"],
    }
    if observed != expected:
        raise ValueError(f"capacity mismatch: expected {expected}, observed {observed}")

    rng = random.Random(seed)
    workload = list(config["workload"])
    rng.shuffle(workload)
    rows: list[dict[str, Any]] = []
    for item in workload:
        name = item["class"]
        if item["deadline_seconds"] <= 0 or item["work_iterations"] <= 0:
            raise ValueError("deadline_seconds and work_iterations must be positive")
        document = f"TaskLoom {name} seed {seed}"
        body = {
            "title": f"WP6 {name}",
            "instruction": "请核对文件数量和词数。",
            "documents": {"notes.txt": document},
            "deadline_seconds": item["deadline_seconds"],
            "work_iterations": item["work_iterations"],
        }
        response = client.post("/api/v1/tasks", headers=HEADERS, json=body)
        response.raise_for_status()
        rows.append(
            {
                "class": name,
                "task_id": response.json()["task_id"],
                "request": body,
                "expected_result": {"files": 1, "words": len(document.split())},
            }
        )

    deadline = time.monotonic() + timeout_seconds
    for row in rows:
        while True:
            response = client.get(f"/api/v1/tasks/{row['task_id']}")
            response.raise_for_status()
            task = response.json()
            if task["outcome"] in TERMINAL:
                result = task.get("result") or {}
                row["outcome"] = task["outcome"]
                row["quality_passed"] = task["outcome"] == "SUCCEEDED" and all(
                    result.get(key) == value for key, value in row["expected_result"].items()
                )
                row["actual_result"] = result
                row["created_at"] = task.get("created_at")
                row["completed_at"] = task.get("completed_at")
                row["deadline_at"] = task.get("deadline_at")
                break
            if time.monotonic() >= deadline:
                row["outcome"] = task["outcome"]
                row["quality_passed"] = False
                row["timed_out_polling"] = True
                break
            time.sleep(0.1)
    return {
        "source": "observed-local-api",
        "scenario": scenario,
        "seed": seed,
        "conditions": observed,
        "platform": {
            "pool_kind": feedback.get("pool_kind"),
            "model": feedback.get("model"),
            "account_max_inflight": feedback.get("account_max_inflight"),
        },
        "policy_claim": "none; capacity labels only",
        "tasks": rows,
    }


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
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
