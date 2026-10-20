"""Contract checks for the explicitly simulated WP6 workload."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.wp6_simulate import run_experiment


def fixture(tmp_path: Path) -> Path:
    path = tmp_path / "calibration.json"
    path.write_text(
        json.dumps(
            {
                "provenance": "synthetic-fixture",
                "tool_seconds": [4, 8],
                "api_seconds": [2, 6],
                "cost_units": [10, 20],
            }
        )
    )
    return path


def test_reproducible_30_day_four_scenarios(tmp_path: Path) -> None:
    calibration = fixture(tmp_path)
    first = run_experiment(calibration, seed=7, days=30, arrivals_per_day=3)
    second = run_experiment(calibration, seed=7, days=30, arrivals_per_day=3)
    assert first == second
    assert set(first) == {"metadata", "events", "summary"}
    assert len(first["summary"]) == 4
    assert len(first["events"]) == 4 * 30 * 3 * 3
    assert all(row["source"] == "simulation" for row in first["events"])
    assert all(row["completed_at_seconds"] <= 30 * 86400 for row in first["summary"])


def test_contention_delays_same_arrivals_and_work(tmp_path: Path) -> None:
    path = fixture(tmp_path)
    raw = json.loads(path.read_text())
    raw["tool_seconds"] = [60, 80]
    raw["api_seconds"] = [60, 80]
    path.write_text(json.dumps(raw))
    result = run_experiment(path, seed=2, days=1, arrivals_per_day=1000)
    by_scenario = {row["scenario"]: row for row in result["summary"]}
    assert (
        by_scenario["tool_constrained"]["mean_tool_wait_seconds"]
        > by_scenario["unconstrained"]["mean_tool_wait_seconds"]
    )
    assert (
        by_scenario["api_constrained"]["mean_api_wait_seconds"]
        > by_scenario["unconstrained"]["mean_api_wait_seconds"]
    )
    assert by_scenario["both_constrained"]["mean_tool_wait_seconds"] > 0
    assert by_scenario["both_constrained"]["mean_api_wait_seconds"] > 0
    assert len({row["total_cost_units"] for row in result["summary"]}) == 1


def test_calibration_requires_explicit_provenance(tmp_path: Path) -> None:
    path = fixture(tmp_path)
    raw = json.loads(path.read_text())
    del raw["provenance"]
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="provenance"):
        run_experiment(path, seed=1, days=1, arrivals_per_day=1)


def test_measured_calibration_requires_source_and_retains_label(tmp_path: Path) -> None:
    path = fixture(tmp_path)
    raw = json.loads(path.read_text())
    raw["provenance"] = "measured"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="source"):
        run_experiment(path, seed=1, days=1, arrivals_per_day=1)
    raw["source"] = "run-2026-10-20.json"
    path.write_text(json.dumps(raw))
    result = run_experiment(path, seed=1, days=1, arrivals_per_day=1)
    assert result["metadata"]["calibration_source"] == raw["source"]
    assert result["metadata"]["source"] == "simulation"
