"""Run one L4 scenario on virtual time and print shares, waits and ledger violations.

uv run --locked python tests/l4/simulate.py --users 8 --tasks 6 --capacity 2
uv run --locked python tests/l4/simulate.py --save trace.json   # keep the workload
uv run --locked python tests/l4/simulate.py --load trace.json --uncoordinated
"""

import argparse
import time
from pathlib import Path

from l4kit import Scenario, Workload, mixed_workload, run_scenario

from taskloom.contracts.models import PlatformConfig


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--users", type=int, default=4)
    parser.add_argument("--tasks", type=int, default=5, help="root tasks per user")
    parser.add_argument(
        "--arrival", type=float, default=10.0, help="mean seconds between a user's tasks"
    )
    parser.add_argument("--capacity", type=int, default=2, help="platform and pool capacity")
    parser.add_argument("--uncoordinated", action="store_true", help="clients ignore feedback")
    parser.add_argument("--horizon", type=float, default=7200, help="virtual seconds before abort")
    parser.add_argument("--save", type=Path, help="write the workload as a JSON trace")
    parser.add_argument("--load", type=Path, help="replay a JSON trace instead of generating")
    args = parser.parse_args()

    workload = (
        Workload.load(args.load)
        if args.load
        else mixed_workload(args.seed, args.users, args.tasks, args.arrival)
    )
    if args.save:
        workload.save(args.save)
    config = PlatformConfig(pool_capacity=args.capacity)
    started = time.monotonic()
    result = run_scenario(
        Scenario(workload, config, coordinated=not args.uncoordinated, horizon_seconds=args.horizon)
    )
    print(result.report.render())
    print(
        f"\nvirtual={result.elapsed:.1f}s wall={time.monotonic() - started:.2f}s "
        f"pool_calls={len(result.pool.log)} pool_peak={result.pool.peak}"
    )
    print("violations:", result.violations or "none")


if __name__ == "__main__":
    main()
