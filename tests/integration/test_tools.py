import json
import os
import subprocess
import sys
from pathlib import Path

from taskloom.contracts.models import digest

SRC = Path(__file__).resolve().parents[2] / "src"


def run_tool(spec_path: Path) -> subprocess.CompletedProcess[bytes]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "taskloom.execution.tool", "--spec", str(spec_path)],
        capture_output=True,
        env=env,
        check=False,
    )


def stage(tmp_path: Path, tool: str, inputs: dict[str, str], arguments: dict | None = None):
    workspace = tmp_path / "workspace"
    (workspace / "inputs").mkdir(parents=True)
    for name, content in inputs.items():
        (workspace / "inputs" / name).write_text(content, encoding="utf-8")
    output = workspace / "result.json"
    spec = {
        "workspace": str(workspace),
        "output": str(output),
        "input_version": digest(inputs),
        "tool": tool,
        "arguments": arguments or {},
        "work_iterations": 1,
        "emit_bytes": 0,
    }
    spec_path = workspace / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    return spec_path, output


def test_list_files_worker_publishes_sha256_rows(tmp_path: Path) -> None:
    inputs = {"a.txt": "alpha", "b.txt": "beta beta"}
    spec_path, output = stage(tmp_path, "list_files", inputs)
    result = run_tool(spec_path)
    assert result.returncode == 0, result.stderr.decode()
    published = json.loads(output.read_text())
    assert published["files"] == 2
    assert {row["name"] for row in published["rows"]} == {"a.txt", "b.txt"}
    assert published["input_version"] == digest(inputs)


def test_top_words_worker_ranks_frequencies(tmp_path: Path) -> None:
    inputs = {"a.txt": "the the the cat", "b.txt": "cat dog"}
    spec_path, output = stage(tmp_path, "top_words", inputs, arguments={"top_n": 2})
    result = run_tool(spec_path)
    assert result.returncode == 0, result.stderr.decode()
    published = json.loads(output.read_text())
    assert published["words"] == 6
    assert published["rows"] == [{"word": "the", "count": 3}, {"word": "cat", "count": 2}]


def test_file_stats_worker_reports_words_and_work_digest(tmp_path: Path) -> None:
    inputs = {"notes.txt": "one two three"}
    spec_path, output = stage(tmp_path, "file_stats", inputs)
    result = run_tool(spec_path)
    assert result.returncode == 0, result.stderr.decode()
    published = json.loads(output.read_text())
    assert published["words"] == 3
    assert published["rows"][0]["bytes"] == len("one two three")
    assert published["work_digest"]
    assert published["input_version"] == digest(inputs)


def test_worker_rejects_unknown_tool(tmp_path: Path) -> None:
    spec_path, _ = stage(tmp_path, "nope", {"a.txt": "data"})
    result = run_tool(spec_path)
    assert result.returncode != 0
    assert b"Unknown tool" in result.stderr
