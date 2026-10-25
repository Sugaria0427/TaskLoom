"""Capture a consistent, provenance-labelled client experiment snapshot."""

from collections.abc import Callable
from typing import Any

JsonObject = dict[str, Any]


def capture(fetch: Callable[[str], object], *, source: str, captured_at: str) -> JsonObject:
    """Read tasks, resources, and every event page from one client profile.

    The events endpoint returns at most 100 rows per request. A full page is
    followed by another request, and non-advancing cursors fail explicitly.
    """
    tasks = fetch("/api/v1/tasks")
    resources = fetch("/api/v1/resources")
    if not isinstance(tasks, list) or not isinstance(resources, dict):
        raise ValueError("Unexpected client snapshot response")

    events: list[JsonObject] = []
    cursor = 0
    while True:
        page = fetch(f"/api/v1/events?after={cursor}")
        if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
            raise ValueError("Unexpected event page response")
        if not page:
            break
        next_cursor = page[-1].get("event_seq")
        if not isinstance(next_cursor, int) or next_cursor <= cursor:
            raise ValueError("Event cursor did not advance")
        events.extend(page)
        cursor = next_cursor
        if len(page) < 100:
            break

    return {
        "schema_version": 1,
        "source": {
            "kind": "real-client-snapshot",
            "url": source,
            "captured_at": captured_at,
        },
        "tasks": tasks,
        "resources": resources,
        "events": events,
    }
