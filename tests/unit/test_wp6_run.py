"""Mixed workload runner contract tests with a local API double."""

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.wp6_run import run_workload


def config() -> dict:
    return {
        "workload": [
            {"class": "short_light", "deadline_seconds": 30, "work_iterations": 1},
            {"class": "short_heavy", "deadline_seconds": 30, "work_iterations": 100},
            {"class": "long_light", "deadline_seconds": 120, "work_iterations": 1},
            {"class": "long_heavy", "deadline_seconds": 120, "work_iterations": 100},
        ],
        "scenarios": {
            "unconstrained": {"tool_slots": 2, "pool_capacity": 2},
            "tool_constrained": {"tool_slots": 1, "pool_capacity": 2},
            "api_constrained": {"tool_slots": 2, "pool_capacity": 1},
            "both_constrained": {"tool_slots": 1, "pool_capacity": 1},
        },
    }


def test_manifest_records_ids_conditions_and_actual_result_checks() -> None:
    posted: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/resources":
            return httpx.Response(200, json={"capacity": {"slots": 2}})
        if request.url.path == "/api/v1/platform":
            return httpx.Response(200, json={"connected": True, "feedback": {"pool_capacity": 2}})
        if request.method == "POST":
            body = json.loads(request.content)
            posted.append(body)
            return httpx.Response(202, json={"task_id": f"task-{len(posted)}"})
        index = int(request.url.path.rsplit("-", 1)[-1]) - 1
        body = posted[index]
        return httpx.Response(
            200,
            json={
                "task_id": f"task-{index + 1}",
                "outcome": "SUCCEEDED",
                "result": {"files": 1, "words": len(body["documents"]["notes.txt"].split())},
            },
        )

    with httpx.Client(
        base_url="http://localhost", transport=httpx.MockTransport(respond)
    ) as client:
        first = run_workload(client, config(), "unconstrained", seed=5, timeout_seconds=1)
        second = run_workload(client, config(), "unconstrained", seed=5, timeout_seconds=1)
    assert [row["class"] for row in first["tasks"]] == [row["class"] for row in second["tasks"]]
    assert first["conditions"] == {"tool_slots": 2, "pool_capacity": 2}
    assert all(
        row["task_id"].startswith("task-") and row["quality_passed"] for row in first["tasks"]
    )
    assert all(row["outcome"] == "SUCCEEDED" for row in first["tasks"])


def test_refuses_mislabeled_capacity() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/resources":
            return httpx.Response(200, json={"capacity": {"slots": 2}})
        return httpx.Response(200, json={"connected": True, "feedback": {"pool_capacity": 2}})

    with httpx.Client(
        base_url="http://localhost", transport=httpx.MockTransport(respond)
    ) as client:
        with pytest.raises(ValueError, match="capacity"):
            run_workload(client, config(), "tool_constrained", seed=5, timeout_seconds=1)


def test_success_outcome_without_correct_result_fails_quality() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/resources":
            return httpx.Response(200, json={"capacity": {"slots": 2}})
        if request.url.path == "/api/v1/platform":
            return httpx.Response(200, json={"connected": True, "feedback": {"pool_capacity": 2}})
        if request.method == "POST":
            return httpx.Response(202, json={"task_id": "task-1"})
        return httpx.Response(
            200,
            json={"task_id": "task-1", "outcome": "SUCCEEDED", "result": {"files": 1, "words": 0}},
        )

    one_task = config()
    one_task["workload"] = one_task["workload"][:1]
    with httpx.Client(
        base_url="http://localhost", transport=httpx.MockTransport(respond)
    ) as client:
        manifest = run_workload(client, one_task, "unconstrained", seed=5, timeout_seconds=1)
    assert manifest["tasks"][0]["outcome"] == "SUCCEEDED"
    assert manifest["tasks"][0]["quality_passed"] is False
