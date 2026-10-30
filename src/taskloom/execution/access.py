"""Resolve local paths consistently across a Windows desktop and its WSL executor."""

import re
from pathlib import Path


def local_path(value: str, workspace: Path) -> Path:
    if re.match(r"^[A-Za-z]:[\\/]", value):
        value = "/mnt/" + value[0].lower() + "/" + value[3:].replace("\\", "/")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else workspace / path).resolve()


def readable(path: Path, workspace: Path, tasks: Path, approved: str | None = None) -> bool:
    if str(path) == approved:
        return True
    if path.is_relative_to(workspace):
        parts = path.relative_to(workspace).parts
    elif path.is_relative_to(tasks):
        parts = path.relative_to(tasks).parts
    else:
        return False
    sensitive = {".git", ".config", ".codex", ".ssh", "agent-workspace"}
    if any(part in sensitive or part.startswith(".env") for part in parts):
        return False
    return True
