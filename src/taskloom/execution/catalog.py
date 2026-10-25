"""Declared resource profiles for the controlled tools (the WP4 resource vector).

Each profile is a *declaration* — the maximum demand a tool may place on the
shared local resources — kept separate from the *measured* results the executor
records on ``ToolState``. Admission uses ``demand``; the cgroup enforces the
memory and CPU bounds from the same profile.
"""

from dataclasses import dataclass
from typing import Literal

from taskloom.domain.model import Resources

MEBIBYTE = 1048576
KIBIBYTE = 1024

ToolName = Literal["file_stats", "grep", "list_files", "top_words"]


@dataclass(frozen=True)
class ToolProfile:
    name: ToolName
    demand: Resources
    cpu_quota: int = 100000  # microseconds per period; 100000 == one full CPU
    cpu_period: int = 100000


TOOL_PROFILES: dict[str, ToolProfile] = {
    "file_stats": ToolProfile(
        name="file_stats",
        demand=Resources(slots=1, memory_bytes=128 * MEBIBYTE, artifact_bytes=3 * MEBIBYTE),
    ),
    "grep": ToolProfile(
        name="grep",
        demand=Resources(slots=1, memory_bytes=128 * MEBIBYTE, artifact_bytes=3 * MEBIBYTE),
    ),
    "list_files": ToolProfile(
        name="list_files",
        demand=Resources(slots=1, memory_bytes=64 * MEBIBYTE, artifact_bytes=256 * KIBIBYTE),
        cpu_quota=20000,
    ),
    "top_words": ToolProfile(
        name="top_words",
        demand=Resources(slots=1, memory_bytes=256 * MEBIBYTE, artifact_bytes=2 * MEBIBYTE),
    ),
}


def tool_names() -> tuple[str, ...]:
    return tuple(TOOL_PROFILES)


def tool_profile(name: str) -> ToolProfile:
    try:
        return TOOL_PROFILES[name]
    except KeyError:
        raise ValueError(f"Unknown tool: {name}") from None


def tool_demand(name: str) -> Resources:
    return tool_profile(name).demand


def max_demand() -> Resources:
    """Element-wise catalog maximum for capacity planning, never an Attempt claim."""
    return Resources(
        slots=max(profile.demand.slots for profile in TOOL_PROFILES.values()),
        memory_bytes=max(profile.demand.memory_bytes for profile in TOOL_PROFILES.values()),
        artifact_bytes=max(profile.demand.artifact_bytes for profile in TOOL_PROFILES.values()),
    )
