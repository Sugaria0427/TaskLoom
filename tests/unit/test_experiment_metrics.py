import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from taskloom.experiments.metrics import summarize_tasks


def task(identity: str, outcome: str, completed: int | None, *, deadline: int = 10) -> dict:
    origin = datetime(2026, 1, 1, tzinfo=UTC)
    return {
        "task_id": identity,
        "tenant_id": "alice",
        "agent_id": identity + "-agent",
        "specification": {},
        "input_version": "v1",
        "outcome": outcome,
        "created_at": origin.isoformat(),
        "deadline_at": (origin + timedelta(seconds=deadline)).isoformat(),
        "completed_at": (origin + timedelta(seconds=completed)).isoformat()
        if completed is not None
        else None,
    }


def test_all_submitted_tasks_are_denominator_and_quality_is_explicit() -> None:
    records = [task("a", "SUCCEEDED", 5), task("b", "SUCCEEDED", 12), task("c", "PENDING", None)]
    report = summarize_tasks(records, quality={"a": True, "b": True}, source="fixture.json")
    assert report["submitted_tasks"] == 3
    assert report["on_time_quality_successes"] == 1
    assert report["on_time_quality_rate"] == 1 / 3
    assert report["outcomes"]["PENDING"] == 1
    assert report["provenance"]["source"] == "fixture.json"


def test_missing_quality_makes_rate_unavailable() -> None:
    report = summarize_tasks([task("a", "SUCCEEDED", 5)], source="fixture.json")
    assert report["on_time_quality_rate"] is None
    assert "on_time_quality_rate" in report["unavailable"]
    assert report["on_time_quality_successes"] == 0


def test_p95_and_outcomes_including_timeout_and_unknown() -> None:
    records = [
        task("a", "SUCCEEDED", 1),
        task("b", "SUCCEEDED", 3),
        task("c", "UNKNOWN", None),
        {**task("d", "FAILED", 11), "error": "DEADLINE_EXPIRED: waiting"},
    ]
    report = summarize_tasks(records, quality={"a": True, "b": True}, source="fixture.json")
    assert report["p95_completion_seconds"] == 3
    assert report["outcomes"]["UNKNOWN"] == 1
    assert report["timeout_count"] == 1
    assert report["background_max_wait_seconds"] is None


def test_duplicate_task_identity_is_rejected() -> None:
    try:
        summarize_tasks([task("a", "SUCCEEDED", 1), task("a", "SUCCEEDED", 1)])
    except ValueError as error:
        assert "duplicate" in str(error).lower()
    else:
        raise AssertionError("duplicate task IDs must fail")


def test_report_cli_writes_deterministic_json_and_csv(tmp_path) -> None:
    source = tmp_path / "tasks.json"
    verdicts = tmp_path / "quality.json"
    output = tmp_path / "report.json"
    table = tmp_path / "tasks.csv"
    source.write_text(json.dumps([task("a", "SUCCEEDED", 5)]), encoding="utf-8")
    verdicts.write_text('{"a": true}', encoding="utf-8")
    script = Path(__file__).resolve().parents[2] / "tools" / "wp6_report.py"
    command = [
        sys.executable,
        str(script),
        str(source),
        "--quality",
        str(verdicts),
        "--json",
        str(output),
        "--csv",
        str(table),
    ]
    subprocess.run(command, check=True)
    first_json = output.read_bytes()
    first_csv = table.read_bytes()
    subprocess.run(command, check=True)
    assert output.read_bytes() == first_json
    assert table.read_bytes() == first_csv
    assert json.loads(first_json)["on_time_quality_rate"] == 1
    assert "a,alice,SUCCEEDED" in first_csv.decode()
