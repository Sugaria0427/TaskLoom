"""A real client/platform/executor run must calibrate into the same workflow shape."""

import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

from taskloom.bootstrap.development import Topology
from taskloom.experiments.capture import capture

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.wp6_calibrate import calibrate
from tools.wp6_run import run_workload, write_manifest
from tools.wp6_simulate import run_experiment


def test_actual_runtime_capture_preserves_model_tool_model(tmp_path: Path) -> None:
    topology = Topology(tmp_path / "runtime", ephemeral=True)
    topology.start()
    try:
        with httpx.Client(base_url=topology.client_url, timeout=10, trust_env=False) as client:
            manifest = run_workload(
                client,
                {
                    "scenarios": {"unconstrained": {"tool_slots": 2, "pool_capacity": 2}},
                    "workload": [
                        {"class": "short_light", "deadline_seconds": 60, "work_iterations": 1}
                    ],
                },
                "unconstrained",
                seed=1,
                repeats=1,
                interarrival_seconds=0,
                checkpoint=lambda value: write_manifest(tmp_path / "run.json", value),
            )
            assert not manifest["incomplete"]
            assert manifest["tasks"][0]["quality_passed"] is True

            def fetch(path: str) -> object:
                reply = client.get(path)
                reply.raise_for_status()
                return reply.json()

            snapshot = capture(
                fetch, source=topology.client_url, captured_at=datetime.now(UTC).isoformat()
            )
        calibration = calibrate(
            snapshot, manifest, source="snapshot.json", manifest_source="run.json"
        )
        workflow = calibration["workflows"][0]
        assert [stage["kind"] for stage in workflow["stages"]] == ["model", "tool", "model"]
        assert sum(stage["cost_units"] for stage in workflow["stages"]) == workflow["cost_units"]
        path = tmp_path / "measured.json"
        write_manifest(path, calibration)
        simulated = run_experiment(path, seed=1, days=1, arrivals_per_day=1)
        assert all(
            row["model_stages"] == 2 and row["tool_stages"] == 1 for row in simulated["summary"]
        )
        assert all(
            row["total_cost_units"] == workflow["cost_units"] for row in simulated["summary"]
        )
    finally:
        topology.close()
