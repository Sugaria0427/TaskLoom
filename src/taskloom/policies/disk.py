"""Finite FIFO/SCAN teaching model: unit seek time, one unit service time."""

import math
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class DiskRequest:
    identity: str
    track: int
    arrival: float = 0


def schedule_disk(
    requests: list[DiskRequest],
    *,
    head: int,
    tracks: int = 200,
    direction: Literal[-1, 1] = 1,
    policy: Literal["fifo", "scan"] = "scan",
) -> dict[str, object]:
    if not 1 <= tracks <= 1000000 or not 0 <= head < tracks:
        raise ValueError("Invalid track domain or initial head")
    if direction not in (-1, 1) or policy not in ("fifo", "scan"):
        raise ValueError("Unknown direction or disk policy")
    if len(requests) > 512 or len({r.identity for r in requests}) != len(requests):
        raise ValueError("Disk trace requires at most 512 unique request identities")
    if any(
        not 0 <= r.track < tracks or not math.isfinite(r.arrival) or r.arrival < 0 for r in requests
    ):
        raise ValueError("Request is outside the track or arrival domain")
    future = sorted(enumerate(requests), key=lambda item: (item[1].arrival, item[0]))
    pending: list[tuple[int, DiskRequest]] = []
    time = 0.0
    position = float(head)
    distance = 0.0
    reversals = 0
    served: list[dict[str, object]] = []
    waits: list[float] = []
    while future or pending:
        while future and future[0][1].arrival <= time:
            pending.append(future.pop(0))
        if not pending:
            time = future[0][1].arrival
            continue
        chosen: tuple[int, DiskRequest] | None
        if policy == "fifo":
            chosen = min(pending, key=lambda item: (item[1].arrival, item[0]))
            movement = abs(chosen[1].track - position)
        else:
            ahead = [item for item in pending if (item[1].track - position) * direction >= 0]
            chosen = (
                min(ahead, key=lambda item: (abs(item[1].track - position), item[0]))
                if ahead
                else None
            )
            target = chosen[1].track if chosen else (tracks - 1 if direction == 1 else 0)
            movement = abs(target - position)
            # A new arrival can be picked up on the way in the current sweep.
            if future and future[0][1].arrival < time + movement:
                elapsed = future[0][1].arrival - time
                position += direction * elapsed
                distance += elapsed
                time += elapsed
                continue
            if chosen is None:
                position = float(target)
                time += movement
                distance += movement
                direction = -1 if direction == 1 else 1
                reversals += 1
                continue
        assert chosen is not None
        time += movement
        distance += movement
        position = float(chosen[1].track)
        waits.append(time - chosen[1].arrival)
        served.append(
            {
                "request_id": chosen[1].identity,
                "track": chosen[1].track,
                "arrival": chosen[1].arrival,
                "started_at": time,
                "wait": time - chosen[1].arrival,
                "finished_at": time + 1,
            }
        )
        time += 1
        pending.remove(chosen)
    return {
        "model": "teaching-disk-unit-seek-and-service",
        "policy": policy,
        "seek_distance": distance,
        "boundary_reversals": reversals,
        "served": served,
        "mean_wait": sum(waits) / len(waits) if waits else 0.0,
        "max_wait": max(waits, default=0.0),
    }
