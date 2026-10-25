from taskloom.domain.model import ResourcePool, Resources


def test_admission_reports_blocking_dimension() -> None:
    pool = ResourcePool(Resources(slots=2, memory_bytes=100, artifact_bytes=1000))
    pool.reserve("a", Resources(slots=1, memory_bytes=80))
    feasible, reason = pool.admission(Resources(slots=1, memory_bytes=30))
    assert not feasible
    assert reason == "short of 10 memory bytes"


def test_admission_returns_no_reason_when_feasible() -> None:
    pool = ResourcePool(Resources(slots=2, memory_bytes=100, artifact_bytes=1000))
    feasible, reason = pool.admission(Resources(slots=1, memory_bytes=30))
    assert feasible
    assert reason is None


def test_admission_reports_capacity_exceeded() -> None:
    pool = ResourcePool(Resources(slots=2, memory_bytes=100, artifact_bytes=1000))
    feasible, reason = pool.admission(Resources(slots=1, memory_bytes=101))
    assert not feasible
    assert reason == "exceeds total capacity"


def test_admission_lists_each_short_dimension() -> None:
    pool = ResourcePool(Resources(slots=2, memory_bytes=100, artifact_bytes=1000))
    pool.reserve("a", Resources(slots=2, memory_bytes=80))
    feasible, reason = pool.admission(Resources(slots=1, memory_bytes=30, artifact_bytes=500))
    assert not feasible
    assert reason == "short of 1 slots, 10 memory bytes"


def test_shortage_reports_deficit_per_dimension() -> None:
    pool = ResourcePool(Resources(slots=2, memory_bytes=100))
    pool.reserve("a", Resources(slots=2, memory_bytes=60))
    assert pool.shortage(Resources(slots=1, memory_bytes=50)) == Resources(slots=1, memory_bytes=10)
