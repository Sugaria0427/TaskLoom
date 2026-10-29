"""Reproduce IKR9I2 resource mechanisms with explicitly labelled evidence."""

import argparse
import asyncio
import json
from pathlib import Path

from taskloom.domain.model import Resources
from taskloom.experiments.resource_bench import protection_trial
from taskloom.policies.disk import DiskRequest, schedule_disk
from taskloom.policies.first_fit import FirstFit
from taskloom.policies.resources import PhaseResourcePool


async def demonstration(repeats: int) -> dict[str, object]:
    pool = PhaseResourcePool(Resources(slots=2, memory_bytes=10))
    for key in ("a", "b"):
        pool.declare(key, Resources(slots=1, memory_bytes=10))
    pool.reserve("a", Resources(slots=1, memory_bytes=4))
    refused = not pool.reserve("b", Resources(slots=1, memory_bytes=4))
    unsafe = pool.snapshot()
    pool.grant("a", Resources(slots=1, memory_bytes=10))
    pool.release("a")
    pool.grant("b", Resources(slots=1, memory_bytes=10))
    pool.release("b")
    arena = FirstFit(100)
    a, b, c = [arena.allocate(30) for _ in range(3)]
    assert a and b and c
    arena.write(b.identity, b"b" * 30)
    arena.release(a.identity)
    arena.release(c.identity)
    fragmented = arena.snapshot()
    arena.release(b.identity)
    requests = [
        DiskRequest(str(i), track) for i, track in enumerate([98, 183, 37, 122, 14, 124, 65, 67])
    ]
    trials = []
    for _ in range(repeats):
        for enabled in (False, True):
            trials.append(await protection_trial(enabled))
    return {
        "schema_version": 1,
        "banker": {
            "source": "production-resource-policy-controlled-trace",
            "unsafe_request_refused": refused,
            "refusal": unsafe,
            "final_available": pool.available.memory_bytes,
            "allocations_remaining": len(pool.allocations),
        },
        "protection_trials": trials,
        "first_fit": {"fragmented": fragmented, "coalesced": arena.snapshot()},
        "disk": [schedule_disk(requests, head=53, policy=policy) for policy in ("fifo", "scan")],
        "physical_execution": "Run integration/test_tool_phases and linux/test_tool_phases; this report does not substitute for those tests.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = asyncio.run(demonstration(args.repeats))
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n")
    print(rendered)


if __name__ == "__main__":
    main()
