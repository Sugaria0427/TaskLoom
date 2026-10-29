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
import stat
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from taskloom.domain.identity import digest
from taskloom.execution.access import AccessPermit, FileLease, check_permit
from taskloom.execution.catalog import tool_names


def read_inputs(inputs: Path) -> dict[str, str]:
    return {path.name: path.read_bytes().decode("utf-8") for path in inputs.iterdir()}


def verify_inputs(spec: dict[str, Any], inputs: Path) -> None:
    """Reject a workspace whose staged contents no longer match its version.

    Args:
        spec: Tool specification carrying the expected input version.
        inputs: Staged input directory.

    Raises:
        ValueError: The staged bytes differ from the declared version.
    """
    if digest(read_inputs(inputs)) != spec["input_version"]:
        raise ValueError("Staged file contents changed")


def check_inputs(inputs: Path) -> None:
    """Reject staged inputs the tool process may not read.

    Args:
        inputs: Staged input directory.

    Raises:
        ValueError: A staged object is not a readable regular file of this user.
    """
    for path in inputs.iterdir():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Staged input is not a regular file")
        if info.st_uid != os.getuid():
            raise ValueError("Staged input belongs to another user")
        if not os.access(path, os.R_OK):
            raise ValueError("Staged input is not readable")


def admit(spec: dict[str, Any]) -> FileLease | None:
    """Check the access permit and hold the resource locks of one attempt.

    Args:
        spec: Tool specification; ``access`` is present when the executor
            admitted this process.

    Returns:
        The lease that must be released after publishing, or ``None`` when the
        module was invoked directly without a coordinator.

    Raises:
        ValueError: The permit is forged, expired, or made out to other terms.
    """
    access = spec.get("access")
    if not access:
        return None
    permit = AccessPermit(**access["permit"])
    secret = Path(access["secret_path"]).read_bytes()
    check_permit(
        permit,
        secret,
        holder=spec.get("attempt_id"),
        input_version=spec["input_version"],
    )
    locks = [(Path(item["path"]), item["mode"]) for item in access["locks"]]
    return FileLease(locks).__enter__()


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
    top_n = spec.get("arguments", {}).get("top_n", 10)
    if type(top_n) is not int or not 1 <= top_n <= 1000:
        raise ValueError("top_n must be an integer between 1 and 1000")
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


def grep(spec: dict[str, Any], inputs: Path) -> dict[str, Any]:
    arguments = spec.get("arguments", {})
    pattern = arguments.get("pattern")
    if not isinstance(pattern, str) or set(arguments) != {"pattern"}:
        raise ValueError("grep requires one string pattern")
    actual = read_inputs(inputs)
    lines: list[dict[str, Any]] = []
    matches = words = 0
    for name in sorted(actual):
        for lineno, line in enumerate(actual[name].splitlines(), 1):
            if pattern in line:
                matches += 1
                words += len(line.split())
                if len(lines) < 64:
                    lines.append({"file": name, "line": lineno, "text": line})
    verify_inputs(spec, inputs)
    return {
        "files": len(actual),
        "words": words,
        "matches": matches,
        "lines": lines,
        "input_version": spec["input_version"],
    }


HANDLERS: dict[str, Callable[[dict[str, Any], Path], dict[str, Any]]] = {
    "file_stats": file_stats,
    "grep": grep,
    "list_files": list_files,
    "top_words": top_words,
}


def publish(spec: dict[str, Any], result: dict[str, Any]) -> None:
    """Publish one tool result atomically.

    Args:
        spec: Tool specification carrying the output path.
        result: Result body to publish.
    """
    target = Path(spec["output"])
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    temporary.replace(target)
    print(json.dumps({"artifact": str(target), "files": result["files"]}), flush=True)


def main() -> None:
    """Run one admitted tool against its staged workspace."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args()
    spec = json.loads(args.spec.read_text())
    tool = spec.get("tool_name", spec.get("tool", "file_stats"))
    if "tool" in spec and "tool_name" in spec and spec["tool"] != spec["tool_name"]:
        raise ValueError("Conflicting tool names")
    if tool not in tool_names():
        raise ValueError(f"Unknown tool: {tool}")
    lease = admit(spec)
    try:
        if spec.get("cgroup_path"):
            (Path(spec["cgroup_path"]) / "cgroup.procs").write_text(str(os.getpid()))
        inputs = Path(spec["workspace"]) / "inputs"
        check_inputs(inputs)
        verify_inputs(spec, inputs)
        result = HANDLERS[tool](spec, inputs)
        emit = spec["emit_bytes"]
        while emit:
            chunk = b"x" * min(4096, emit)
            os.write(2, chunk)
            emit -= len(chunk)
        verify_inputs(spec, inputs)
        publish(spec, result)
    finally:
        if lease is not None:
            lease.__exit__(None, None, None)


if __name__ == "__main__":
    main()
