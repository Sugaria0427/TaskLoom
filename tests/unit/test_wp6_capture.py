from taskloom.experiments.capture import capture


def test_capture_paginates_events_and_preserves_source() -> None:
    calls: list[str] = []

    def fetch(path: str) -> object:
        calls.append(path)
        if path == "/api/v1/tasks":
            return [{"task_id": "task-1", "tenant_id": "alice"}]
        if path == "/api/v1/resources":
            return {"tool": {"capacity": 2}}
        if path == "/api/v1/events?after=0":
            return [{"event_seq": index} for index in range(1, 101)]
        if path == "/api/v1/events?after=100":
            return [{"event_seq": 101}]
        raise AssertionError(path)

    result = capture(fetch, source="http://127.0.0.1:8795", captured_at="2026-10-20T00:00:00Z")

    assert len(result["events"]) == 101
    assert result["events"][-1]["event_seq"] == 101
    assert result["source"] == {
        "kind": "real-client-snapshot",
        "url": "http://127.0.0.1:8795",
        "captured_at": "2026-10-20T00:00:00Z",
    }
    assert calls[-1] == "/api/v1/events?after=100"


def test_capture_rejects_non_advancing_event_cursor() -> None:
    def fetch(path: str) -> object:
        if path == "/api/v1/tasks":
            return []
        if path == "/api/v1/resources":
            return {}
        if path.startswith("/api/v1/events"):
            return [{"event_seq": 1}] * 100
        raise AssertionError(path)

    try:
        capture(fetch, source="local", captured_at="2026-10-20T00:00:00Z")
    except ValueError as error:
        assert "cursor" in str(error)
    else:
        raise AssertionError("Expected a non-advancing cursor to fail")
