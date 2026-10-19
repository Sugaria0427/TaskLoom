"""Registered subprocess tools; each performs real file I/O on the staged inputs."""

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import TypedDict, cast

from taskloom.domain.identity import digest


class FileStat(TypedDict):
    name: str
    bytes: int
    words: int
    sha256: str


class GrepMatch(TypedDict):
    file: str
    line: int
    text: str


class ToolSpecInput(TypedDict):
    workspace: str
    output: str
    input_version: str
    work_iterations: int
    emit_bytes: int
    cgroup_path: str | None
    tool_name: str
    arguments: dict[str, object]


def run_file_stats(spec: ToolSpecInput, inputs: Path) -> dict[str, object]:
    """Return verified per-file statistics for every staged document."""
    rows: list[FileStat] = []
    combined = hashlib.sha256()
    for path in sorted(inputs.iterdir()):
        data = path.read_bytes()
        text = data.decode("utf-8")
        rows.append(
            {
                "name": path.name,
                "bytes": len(data),
                "words": len(text.split()),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
        combined.update(data)
    value = combined.digest()
    for index in range(spec["work_iterations"]):
        value = hashlib.sha256(value + index.to_bytes(8, "little")).digest()
    return {
        "files": len(rows),
        "words": sum(row["words"] for row in rows),
        "rows": rows,
        "work_digest": value.hex(),
        "input_version": spec["input_version"],
    }


def run_grep(spec: ToolSpecInput, actual: dict[str, str]) -> dict[str, object]:
    """Search the staged documents for a pattern and report verified matches."""
    pattern = str(spec.get("arguments", {}).get("pattern", ""))
    matches: list[GrepMatch] = []
    words = 0
    for name in sorted(actual):
        for lineno, line in enumerate(actual[name].splitlines(), 1):
            if pattern in line:
                matches.append({"file": name, "line": lineno, "text": line})
                words += len(line.split())
    return {
        "files": len(actual),
        "words": words,
        "matches": len(matches),
        "lines": matches[:64],
        "input_version": spec["input_version"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args()
    spec = cast(ToolSpecInput, json.loads(args.spec.read_text()))
    cgroup = spec.get("cgroup_path")
    if cgroup:
        (Path(cgroup) / "cgroup.procs").write_text(str(os.getpid()))
    workspace = Path(spec["workspace"])
    inputs = workspace / "inputs"
    actual = {path.name: path.read_bytes().decode("utf-8") for path in inputs.iterdir()}
    if digest(actual) != spec["input_version"]:
        raise ValueError("Staged file contents changed")
    if spec.get("tool_name") == "grep":
        result = run_grep(spec, actual)
    else:
        result = run_file_stats(spec, inputs)
    emit = spec["emit_bytes"]
    while emit:
        chunk = b"x" * min(4096, emit)
        os.write(2, chunk)
        emit -= len(chunk)
    target = Path(spec["output"])
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    temporary.replace(target)
    print(json.dumps({"artifact": str(target)}), flush=True)


if __name__ == "__main__":
    main()
