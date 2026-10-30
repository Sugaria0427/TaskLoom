import pytest

from taskloom.policies.disk import DiskRequest, schedule_disk
from taskloom.policies.first_fit import FirstFit


def test_first_fit_fragmentation_reuse_actual_bytes_and_adjacent_coalescing():
    arena = FirstFit(100)
    a, b, c = [arena.allocate(30) for _ in range(3)]
    arena.write(b.identity, b"b" * 30)
    arena.release(a.identity)
    arena.release(c.identity)
    assert arena.snapshot()["free_bytes"] == 70
    assert arena.snapshot()["largest_free_bytes"] == 40
    assert arena.allocate(50) is None  # enough total, no contiguous block
    d = arena.allocate(20)
    assert d.offset == 0  # first adequate hole, rather than the largest hole
    assert arena.read(b.identity) == b"b" * 30
    with pytest.raises(ValueError):
        arena.write(d.identity, b"x" * 21)
    arena.release(d.identity)
    arena.release(b.identity)
    assert arena.free_blocks == [(0, 100)]
    with pytest.raises(KeyError):
        arena.release(b.identity)


def test_scan_reaches_boundary_before_reversing_and_fifo_uses_same_trace():
    trace = [DiskRequest("a", 20), DiskRequest("b", 80)]
    scan = schedule_disk(trace, head=50, tracks=100)
    fifo = schedule_disk(trace, head=50, tracks=100, policy="fifo")
    assert [r["request_id"] for r in scan["served"]] == ["b", "a"]
    assert scan["seek_distance"] == 128  # 50 -> 80 -> boundary 99 -> 20
    assert fifo["seek_distance"] == 90  # 50 -> 20 -> 80
    assert scan["boundary_reversals"] == 1
    assert [r.track for r in trace] == [20, 80]


def test_scan_admits_dynamic_arrivals_in_sweep_but_not_before_arrival():
    trace = [DiskRequest("far", 90), DiskRequest("near", 60, 5), DiskRequest("behind", 20, 6)]
    result = schedule_disk(trace, head=50, tracks=100)
    assert [r["request_id"] for r in result["served"]] == ["near", "far", "behind"]
    assert all(r["started_at"] >= r["arrival"] for r in result["served"])
    assert result["seek_distance"] == 128
    assert schedule_disk([], head=0)["seek_distance"] == 0
    same = schedule_disk([DiskRequest("a", 0, 5), DiskRequest("b", 0, 5)], head=0)
    assert [r["wait"] for r in same["served"]] == [0, 1]


@pytest.mark.parametrize(
    "trace",
    [
        [DiskRequest("x", 200)],
        [DiskRequest("x", 0, -1)],
        [DiskRequest("x", 0, float("nan"))],
        [DiskRequest("x", 0), DiskRequest("x", 1)],
    ],
)
def test_disk_rejects_invalid_or_ambiguous_traces(trace):
    with pytest.raises(ValueError):
        schedule_disk(trace, head=50)
