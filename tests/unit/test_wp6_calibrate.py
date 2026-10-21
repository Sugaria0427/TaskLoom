"""Measured WP6 calibration excludes unsupported intervals and costs."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.wp6_calibrate import calibrate
from tools.wp6_simulate import run_experiment


def event(kind: str, state: str, attempt: str, at: str) -> dict:
    return {
        "root_task_id": "task-1",
        "attempt_id": attempt,
        "type": f"{kind}.{state}",
        "outcome": "SUCCEEDED" if state == "finished" else "PENDING",
        "occurred_at": at,
    }


def valid_inputs() -> tuple[dict, dict]:
    snapshot = {
        "source": {"kind": "real-client-snapshot", "url": "http://localhost"},
        "tasks": [
            {
                "task_id": "task-1",
                "outcome": "SUCCEEDED",
                "quiescent": True,
                "reserved_units": 0,
                "consumed_units": 40,
            }
        ],
        "events": [
            event("model", "running", "m1", "2026-10-20T00:00:00Z"),
            event("model", "finished", "m1", "2026-10-20T00:00:03Z"),
            event("tool", "running", "t1", "2026-10-20T00:00:04Z"),
            event("tool", "running", "t1", "2026-10-20T00:00:05Z"),
            event("tool", "finished", "t1", "2026-10-20T00:00:10Z"),
        ],
    }
    manifest = {
        "incomplete": False,
        "tasks": [{"task_id": "task-1", "outcome": "SUCCEEDED", "quality_passed": True}],
    }
    return snapshot, manifest


def test_derives_measured_samples_and_deduplicates_running_state() -> None:
    snapshot, manifest = valid_inputs()
    result = calibrate(snapshot, manifest, source="snapshot.json", manifest_source="run.json")
    assert result["provenance"] == "measured"
    assert result["source"] == "snapshot.json"
    assert result["tool_seconds"] == [6.0]
    assert result["api_seconds"] == [3.0]
    assert result["cost_units"] == [40]
    assert result["sample_counts"] == {"tool": 1, "api": 1, "cost": 1}


def test_excludes_waits_unknown_attempt_and_unrelated_task() -> None:
    snapshot, manifest = valid_inputs()
    snapshot["events"].insert(0, event("tool", "wait_resource", "t1", "2026-10-19T23:00:00Z"))
    snapshot["events"].append(event("tool", "running", "unknown", "2026-10-20T00:00:00Z"))
    unrelated = event("model", "running", "other", "2026-10-20T00:00:00Z")
    unrelated["root_task_id"] = "other-task"
    snapshot["events"].append(unrelated)
    result = calibrate(snapshot, manifest, source="snapshot.json", manifest_source="run.json")
    assert result["tool_seconds"] == [6.0]
    assert result["api_seconds"] == [3.0]


@pytest.mark.parametrize("problem", ["no-tool", "reversed", "double-finish", "unsettled"])
def test_rejects_missing_or_ambiguous_measurements(problem: str) -> None:
    snapshot, manifest = valid_inputs()
    if problem == "no-tool":
        snapshot["events"] = [row for row in snapshot["events"] if row["type"] != "tool.finished"]
    elif problem == "reversed":
        snapshot["events"][-1]["occurred_at"] = "2026-10-20T00:00:01Z"
    elif problem == "double-finish":
        snapshot["events"].append(dict(snapshot["events"][-1]))
    else:
        snapshot["tasks"][0]["reserved_units"] = 10
    with pytest.raises(ValueError):
        calibrate(snapshot, manifest, source="snapshot.json", manifest_source="run.json")


def test_rejects_incomplete_manifest() -> None:
    snapshot, manifest = valid_inputs()
    manifest["incomplete"] = True
    with pytest.raises(ValueError, match="incomplete"):
        calibrate(snapshot, manifest, source="snapshot.json", manifest_source="run.json")


def test_calibration_is_accepted_by_simulator(tmp_path: Path) -> None:
    snapshot, manifest = valid_inputs()
    calibration = calibrate(snapshot, manifest, source="snapshot.json", manifest_source="run.json")
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(calibration))
    simulated = run_experiment(path, seed=1, days=1, arrivals_per_day=1)
    assert simulated["metadata"]["calibration_provenance"] == "measured"
    assert simulated["metadata"]["source"] == "simulation"


def test_manifest_unknown_quality_fails_even_without_incomplete_flag() -> None:
    snapshot, manifest = valid_inputs()
    manifest["tasks"][0]["quality_passed"] = None
    with pytest.raises(ValueError, match="incomplete"):
        calibrate(snapshot, manifest, source="snapshot.json", manifest_source="run.json")


def test_malformed_interval_is_excluded_if_other_samples_remain() -> None:
    snapshot, manifest = valid_inputs()
    bad = event("tool", "running", "bad", "2026-10-20T00:00:10Z")
    snapshot["events"].extend([bad, event("tool", "finished", "bad", "2026-10-20T00:00:09Z")])
    result = calibrate(snapshot, manifest, source="snapshot.json", manifest_source="run.json")
    assert result["tool_seconds"] == [6.0]
    assert result["excluded_attempts"]["malformed_interval"] == 1
