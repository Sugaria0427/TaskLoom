"""Aggregate exported TaskState records without inferring missing measurements."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from math import ceil
from typing import Any

from taskloom.contracts.models import TaskState
from taskloom.domain.model import Outcome


def _elapsed(start: str, end: str) -> float:
    duration = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    if duration < 0:
        raise ValueError("completion precedes submission")
    return duration


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[ceil(0.95 * len(ordered)) - 1]


def summarize_tasks(
    records: Sequence[Mapping[str, Any]],
    *,
    quality: Mapping[str, bool] | None = None,
    source: str = "unspecified",
) -> dict[str, Any]:
    """Summarize a complete task snapshot; quality verdicts come from external review."""
    tasks = [TaskState.model_validate(record) for record in records]
    identities = [task.task_id for task in tasks]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate task_id in export")
    verdicts = quality or {}
    unknown_verdicts = set(verdicts) - set(identities)
    if unknown_verdicts:
        raise ValueError("quality verdict refers to an unknown task_id")

    outcomes = {outcome.value: 0 for outcome in Outcome}
    completed_durations: list[float] = []
    on_time_quality = 0
    missing_quality = 0
    timeout_count = 0
    rows: list[dict[str, Any]] = []
    for task in sorted(tasks, key=lambda item: item.task_id):
        outcomes[task.outcome.value] += 1
        elapsed = _elapsed(task.created_at, task.completed_at) if task.completed_at else None
        on_time = bool(
            task.completed_at
            and datetime.fromisoformat(task.completed_at)
            <= datetime.fromisoformat(task.deadline_at)
        )
        if task.outcome == Outcome.SUCCEEDED and elapsed is not None:
            completed_durations.append(elapsed)
            if on_time and task.task_id not in verdicts:
                missing_quality += 1
        timed_out = bool(task.error and task.error.startswith("DEADLINE_EXPIRED:"))
        timeout_count += int(timed_out)
        qualified = verdicts.get(task.task_id)
        success = task.outcome == Outcome.SUCCEEDED and on_time and qualified is True
        on_time_quality += int(success)
        rows.append(
            {
                "task_id": task.task_id,
                "tenant_id": task.tenant_id,
                "outcome": task.outcome.value,
                "duration_seconds": elapsed,
                "on_time": on_time if task.completed_at else None,
                "quality_passed": qualified,
                "on_time_quality_success": success,
                "timed_out": timed_out,
                "consumed_units": task.consumed_units,
                "reserved_units": task.reserved_units,
            }
        )

    unavailable = {
        "background_max_wait_seconds": "TaskState has no background classification or wait interval",
        "resource_failure_rate": "TaskState does not distinguish all resource failures",
        "actual_cost": "consumed_units are not a monetary charge",
    }
    if missing_quality:
        unavailable["on_time_quality_rate"] = (
            f"{missing_quality} succeeded tasks lack quality verdicts"
        )
    if not completed_durations:
        unavailable["p95_completion_seconds"] = "no succeeded tasks have completion timestamps"
    submitted = len(tasks)
    if not submitted:
        unavailable["on_time_quality_rate"] = "no submitted tasks"
    return {
        "schema_version": 1,
        "provenance": {
            "source": source,
            "record_type": "TaskState snapshot",
            "quality_verdicts": "external keyed verdicts"
            if quality is not None
            else "not supplied",
        },
        "submitted_tasks": submitted,
        "on_time_quality_successes": on_time_quality,
        "on_time_quality_rate": (
            on_time_quality / submitted if submitted and not missing_quality else None
        ),
        "p95_completion_seconds": _p95(completed_durations),
        "background_max_wait_seconds": None,
        "outcomes": outcomes,
        "timeout_count": timeout_count,
        "unavailable": unavailable,
        "tasks": rows,
    }
