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


def _manifest(*rows: dict) -> dict:
    return {
        "source": "observed-local-api",
        "scenario": "unconstrained",
        "seed": 7,
        "tasks": list(rows),
    }


def _run_manifest_report(
    tmp_path: Path, records: list[dict], manifest: dict
) -> subprocess.CompletedProcess[str]:
    snapshot = tmp_path / "capture.json"
    run = tmp_path / "run.json"
    report = tmp_path / "report.json"
    snapshot.write_text(json.dumps({"tasks": records}), encoding="utf-8")
    run.write_text(json.dumps(manifest), encoding="utf-8")
    script = Path(__file__).resolve().parents[2] / "tools" / "wp6_report.py"
    return subprocess.run(
        [
            sys.executable,
            str(script),
            str(snapshot),
            "--run-manifest",
            str(run),
            "--json",
            str(report),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_run_manifest_limits_denominator_and_supplies_quality(tmp_path: Path) -> None:
    result = _run_manifest_report(
        tmp_path,
        [task("historical", "FAILED", 15), task("selected", "SUCCEEDED", 5)],
        _manifest({"task_id": "selected", "quality_passed": True}),
    )
    assert result.returncode == 0, result.stderr
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["submitted_tasks"] == 1
    assert report["on_time_quality_rate"] == 1
    assert [row["task_id"] for row in report["tasks"]] == ["selected"]
    assert report["provenance"]["run_manifest"] == str(tmp_path / "run.json")


def test_run_manifest_rejects_missing_and_duplicate_ids(tmp_path: Path) -> None:
    selected = {"task_id": "selected", "quality_passed": True}
    cases = [
        ([task("other", "SUCCEEDED", 5)], _manifest(selected)),
        ([task("selected", "SUCCEEDED", 5)], _manifest(selected, selected)),
        ([task("selected", "SUCCEEDED", 5), task("selected", "SUCCEEDED", 5)], _manifest(selected)),
    ]
    for records, manifest in cases:
        result = _run_manifest_report(tmp_path, records, manifest)
        assert result.returncode != 0
        assert "task_id" in result.stderr


def test_run_manifest_rejects_missing_quality_verdict(tmp_path: Path) -> None:
    result = _run_manifest_report(
        tmp_path, [task("selected", "SUCCEEDED", 5)], _manifest({"task_id": "selected"})
    )
    assert result.returncode != 0
    assert "quality_passed" in result.stderr


def test_run_manifest_rejects_incomplete_null_verdict_with_recovery_guidance(
    tmp_path: Path,
) -> None:
    result = _run_manifest_report(
        tmp_path,
        [task("selected", "PENDING", None)],
        _manifest({"task_id": "selected", "quality_passed": None, "incomplete": True}),
    )
    assert result.returncode != 0
    assert "selected" in result.stderr
    assert "recapture" in result.stderr.lower()
    assert not (tmp_path / "report.json").exists()


def test_run_manifest_and_quality_flags_are_exclusive(tmp_path: Path) -> None:
    result = _run_manifest_report(
        tmp_path,
        [task("selected", "SUCCEEDED", 5)],
        _manifest({"task_id": "selected", "quality_passed": True}),
    )
    assert result.returncode == 0
    script = Path(__file__).resolve().parents[2] / "tools" / "wp6_report.py"
    conflict = subprocess.run(
        [
            sys.executable,
            str(script),
            str(tmp_path / "capture.json"),
            "--run-manifest",
            str(tmp_path / "run.json"),
            "--quality",
            str(tmp_path / "run.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert conflict.returncode != 0
    assert "not allowed with argument" in conflict.stderr


def test_rejected_submission_stays_in_run_success_denominator(tmp_path: Path) -> None:
    result = _run_manifest_report(
        tmp_path,
        [task("accepted", "SUCCEEDED", 5)],
        _manifest(
            {"task_id": "accepted", "quality_passed": True, "submission_state": "ACCEPTED"},
            {"submission_state": "REJECTED", "outcome": "REJECTED", "quality_passed": False},
        ),
    )
    assert result.returncode == 0, result.stderr
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["accepted_tasks"] == 1
    assert report["submitted_tasks"] == 2
    assert report["rejected_submissions"] == 1
    assert report["on_time_quality_rate"] == 0.5
