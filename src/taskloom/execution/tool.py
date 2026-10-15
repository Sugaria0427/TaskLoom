"""A registered subprocess tool; it performs real file I/O and bounded hashing."""

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import TypedDict

from taskloom.domain.identity import digest


class FileStat(TypedDict):
    name: str
    bytes: int
    words: int
    sha256: str


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args()
    spec = json.loads(args.spec.read_text())
    if spec.get("cgroup_path"):
        (Path(spec["cgroup_path"]) / "cgroup.procs").write_text(str(os.getpid()))
    workspace = Path(spec["workspace"])
    inputs = workspace / "inputs"
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
    actual = {path.name: path.read_bytes().decode("utf-8") for path in inputs.iterdir()}
    if digest(actual) != spec["input_version"]:
        raise ValueError("Staged file contents changed")
    value = combined.digest()
    for index in range(spec["work_iterations"]):
        value = hashlib.sha256(value + index.to_bytes(8, "little")).digest()
    emit = spec["emit_bytes"]
    while emit:
        chunk = b"x" * min(4096, emit)
        os.write(2, chunk)
        emit -= len(chunk)
    result = {
        "files": len(rows),
        "words": sum(row["words"] for row in rows),
        "rows": rows,
        "work_digest": value.hex(),
        "input_version": spec["input_version"],
    }
    target = Path(spec["output"])
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    temporary.replace(target)
    print(json.dumps({"artifact": str(target), "files": len(rows)}), flush=True)


if __name__ == "__main__":
    main()
