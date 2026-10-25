"""Mixed workload runner contract tests with a local API double."""

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.wp6_run import plan_workload, run_workload


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
            return httpx.Response(
                200, json={"capacity": {"slots": 2}, "model_capacity": {"slots": 2}}
            )
        if request.url.path == "/api/v1/platform":
            return httpx.Response(
                200,
                json={
                    "connected": True,
                    "feedback": {"pool_capacity": 2, "account_max_inflight": 2},
                },
            )
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
        first = run_workload(
            client,
            config(),
            "unconstrained",
            seed=5,
            timeout_seconds=1,
            repeats=1,
            interarrival_seconds=0,
        )
        second = run_workload(
            client,
            config(),
            "unconstrained",
            seed=5,
            timeout_seconds=1,
            repeats=1,
            interarrival_seconds=0,
        )
    assert [row["class"] for row in first["tasks"]] == [row["class"] for row in second["tasks"]]
    assert first["conditions"] == {
        "tool_slots": 2,
        "pool_capacity": 2,
        "account_max_inflight": 2,
        "local_model_slots": 2,
    }
    assert all(
        row["task_id"].startswith("task-") and row["quality_passed"] for row in first["tasks"]
    )
    assert all(row["outcome"] == "SUCCEEDED" for row in first["tasks"])


def test_refuses_mislabeled_capacity() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/resources":
            return httpx.Response(
                200, json={"capacity": {"slots": 2}, "model_capacity": {"slots": 2}}
            )
        return httpx.Response(
            200,
            json={"connected": True, "feedback": {"pool_capacity": 2, "account_max_inflight": 2}},
        )

    with httpx.Client(
        base_url="http://localhost", transport=httpx.MockTransport(respond)
    ) as client:
        with pytest.raises(ValueError, match="capacity"):
            run_workload(client, config(), "tool_constrained", seed=5, timeout_seconds=1)


def test_success_outcome_without_correct_result_fails_quality() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/resources":
            return httpx.Response(
                200, json={"capacity": {"slots": 2}, "model_capacity": {"slots": 2}}
            )
        if request.url.path == "/api/v1/platform":
            return httpx.Response(
                200,
                json={
                    "connected": True,
                    "feedback": {"pool_capacity": 2, "account_max_inflight": 2},
                },
            )
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
        manifest = run_workload(
            client,
            one_task,
            "unconstrained",
            seed=5,
            timeout_seconds=1,
            repeats=1,
            interarrival_seconds=0,
        )
    assert manifest["tasks"][0]["outcome"] == "SUCCEEDED"
    assert manifest["tasks"][0]["quality_passed"] is False


def test_fixed_repeated_submission_plan() -> None:
    first = plan_workload(config(), seed=5, repeats=4, interarrival_seconds=0.25)
    second = plan_workload(config(), seed=5, repeats=4, interarrival_seconds=0.25)
    assert first == second
    assert len(first) == 16
    assert [row["planned_submission_offset_seconds"] for row in first] == [
        index * 0.25 for index in range(16)
    ]
    assert {row["class"] for row in first} == {
        "short_light",
        "short_heavy",
        "long_light",
        "long_heavy",
    }


def test_pending_after_poll_timeout_is_unknown_quality() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/resources":
            return httpx.Response(
                200, json={"capacity": {"slots": 2}, "model_capacity": {"slots": 2}}
            )
        if request.url.path == "/api/v1/platform":
            return httpx.Response(
                200,
                json={
                    "connected": True,
                    "feedback": {"pool_capacity": 2, "account_max_inflight": 2},
                },
            )
        if request.method == "POST":
            return httpx.Response(202, json={"task_id": "task-1"})
        return httpx.Response(200, json={"task_id": "task-1", "outcome": "PENDING"})

    one_task = config()
    one_task["workload"] = one_task["workload"][:1]
    with httpx.Client(
        base_url="http://localhost", transport=httpx.MockTransport(respond)
    ) as client:
        manifest = run_workload(
            client,
            one_task,
            "unconstrained",
            seed=5,
            timeout_seconds=0.001,
            repeats=1,
            interarrival_seconds=0,
        )
    assert manifest["incomplete"] is True
    assert manifest["tasks"][0]["quality_passed"] is None
    assert manifest["tasks"][0]["timed_out_polling"] is True


@pytest.mark.parametrize("bottleneck", ["account", "local"])
def test_refuses_hidden_api_concurrency_limit(bottleneck: str) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/resources":
            return httpx.Response(
                200,
                json={
                    "capacity": {"slots": 2},
                    "model_capacity": {"slots": 1 if bottleneck == "local" else 2},
                },
            )
        return httpx.Response(
            200,
            json={
                "connected": True,
                "feedback": {
                    "pool_capacity": 2,
                    "account_max_inflight": 1 if bottleneck == "account" else 2,
                },
            },
        )

    with httpx.Client(
        base_url="http://localhost", transport=httpx.MockTransport(respond)
    ) as client:
        with pytest.raises(ValueError, match="hidden API capacity"):
            run_workload(client, config(), "unconstrained", seed=5, timeout_seconds=1)


@pytest.mark.parametrize("failure", ["reject", "transport", "server"])
def test_submission_failure_retains_plan_and_collects_other_accepted_tasks(failure: str) -> None:
    posted = 0
    saved: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal posted
        if request.url.path == "/api/v1/resources":
            return httpx.Response(
                200, json={"capacity": {"slots": 2}, "model_capacity": {"slots": 2}}
            )
        if request.url.path == "/api/v1/platform":
            return httpx.Response(
                200,
                json={
                    "connected": True,
                    "feedback": {
                        "pool_capacity": 2,
                        "account_max_inflight": 2,
                    },
                },
            )
        if request.method == "POST":
            posted += 1
            # The durable snapshot already includes this send intent and the full plan.
            assert len(saved[-1]["tasks"]) == 4
            assert saved[-1]["tasks"][posted - 1]["submission_state"] == "SUBMITTING"
            if posted == 2:
                if failure == "transport":
                    raise httpx.ReadTimeout("ambiguous send", request=request)
                return httpx.Response(429 if failure == "reject" else 503, json={"error": "full"})
            return httpx.Response(202, json={"task_id": f"task-{posted}"})
        return httpx.Response(
            200, json={"outcome": "SUCCEEDED", "result": {"files": 1, "words": 5}}
        )

    with httpx.Client(
        base_url="http://localhost", transport=httpx.MockTransport(respond)
    ) as client:
        manifest = run_workload(
            client,
            config(),
            "unconstrained",
            seed=1,
            repeats=1,
            interarrival_seconds=0,
            checkpoint=lambda value: saved.append(json.loads(json.dumps(value))),
        )
    assert posted == 4  # No automatic replay of the ambiguous second submission.
    assert len(manifest["tasks"]) == 4
    failed = manifest["tasks"][1]
    assert failed["submission_state"] == ("REJECTED" if failure == "reject" else "UNKNOWN")
    assert all(row.get("outcome") == "SUCCEEDED" for row in manifest["tasks"] if row.get("task_id"))
    assert manifest["incomplete"] is (failure != "reject")
    assert saved[-1] == manifest
    assert len({row["observation_id"] for row in manifest["tasks"]}) == 4
