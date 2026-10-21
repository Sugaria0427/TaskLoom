"""Registered subprocess tools; each performs real, verifiable file I/O.

The parent executor launches this module with a ``--spec`` describing the tool,
its resource limits and the staged workspace. ``main`` validates the requested
tool against the catalog and dispatches to the matching handler, so the set of
runnable tools is explicitly controlled and each result is independently
recomputable from the staged inputs.
"""

import argparse
import hashlib
import json
import os
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from taskloom.domain.identity import digest
from taskloom.execution.catalog import tool_names


def read_inputs(inputs: Path) -> dict[str, str]:
    return {path.name: path.read_bytes().decode("utf-8") for path in inputs.iterdir()}


def verify_inputs(spec: dict[str, Any], inputs: Path) -> None:
    if digest(read_inputs(inputs)) != spec["input_version"]:
        raise ValueError("Staged file contents changed")


def file_stats(spec: dict[str, Any], inputs: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
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
    verify_inputs(spec, inputs)
    value = combined.digest()
    for index in range(spec["work_iterations"]):
        value = hashlib.sha256(value + index.to_bytes(8, "little")).digest()
    emit = spec["emit_bytes"]
    while emit:
        chunk = b"x" * min(4096, emit)
        os.write(2, chunk)
        emit -= len(chunk)
    return {
        "files": len(rows),
        "words": sum(row["words"] for row in rows),
        "rows": rows,
        "work_digest": value.hex(),
        "input_version": spec["input_version"],
    }


def list_files(spec: dict[str, Any], inputs: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for path in sorted(inputs.iterdir()):
        data = path.read_bytes()
        rows.append(
            {"name": path.name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        )
    verify_inputs(spec, inputs)
    return {"files": len(rows), "rows": rows, "input_version": spec["input_version"]}


def top_words(spec: dict[str, Any], inputs: Path) -> dict[str, Any]:
    top_n = int(spec.get("arguments", {}).get("top_n", 10))
    contents = read_inputs(inputs)
    counts: Counter[str] = Counter()
    total = 0
    for content in contents.values():
        words = content.split()
        total += len(words)
        counts.update(words)
    verify_inputs(spec, inputs)
    rows = [{"word": word, "count": count} for word, count in counts.most_common(top_n)]
    return {
        "files": len(contents),
        "words": total,
        "rows": rows,
        "input_version": spec["input_version"],
    }


HANDLERS: dict[str, Callable[[dict[str, Any], Path], dict[str, Any]]] = {
    "file_stats": file_stats,
    "list_files": list_files,
    "top_words": top_words,
}


def publish(spec: dict[str, Any], result: dict[str, Any]) -> None:
    target = Path(spec["output"])
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    temporary.replace(target)
    print(json.dumps({"artifact": str(target), "files": result["files"]}), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args()
    spec = json.loads(args.spec.read_text())
    tool = spec.get("tool", "file_stats")
    if tool not in tool_names():
        raise ValueError(f"Unknown tool: {tool}")
    if spec.get("cgroup_path"):
        (Path(spec["cgroup_path"]) / "cgroup.procs").write_text(str(os.getpid()))
    inputs = Path(spec["workspace"]) / "inputs"
    publish(spec, HANDLERS[tool](spec, inputs))


if __name__ == "__main__":
    main()
