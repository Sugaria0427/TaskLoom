"""Bounded worker/executor rendezvous inside the owned Attempt directory."""

import json
import os
import time
from pathlib import Path
from typing import Any

from taskloom.contracts.models import ToolPhase


def request_memory(spec: dict[str, Any], version: int) -> None:
    directory = Path(spec["output"]).parent
    request = ToolPhase(
        attempt_id=spec["attempt_id"],
        generation=spec["generation"],
        version=version,
        memory_bytes=(128 + version * 128) * 1048576,
    )
    target = directory / "phase-request.json"
    temporary = target.with_suffix(".tmp")
    with temporary.open("w") as stream:
        stream.write(request.model_dump_json())
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(target)
    deadline = time.monotonic() + spec["timeout_seconds"]
    grant_path = directory / "phase-grant.json"
    while time.monotonic() < deadline:
        if grant_path.exists():
            grant = ToolPhase.model_validate(json.loads(grant_path.read_text()))
            if grant == request:
                return
            if grant.version >= version:
                raise ValueError("Phase grant differs from the original request")
        time.sleep(0.01)
    raise TimeoutError("Phase resource grant did not arrive before timeout")
