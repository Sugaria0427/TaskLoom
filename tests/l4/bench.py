"""L4 benchmark: general samples and adversarial probes, scored by l4bench/scoring.py.

uv run --locked python tests/l4/bench.py run                  # full benchmark
uv run --locked python tests/l4/bench.py run --quick          # smoke pass, a fraction of runs
uv run --locked python tests/l4/bench.py run --policy drr     # occupancy-fair gateway policy
uv run --locked python tests/l4/bench.py run --placement rotate  # pool places, gateway does not
uv run --locked python tests/l4/bench.py compare OLD NEW      # paired score difference
uv run --locked python tests/l4/bench.py facts                # recheck constants against traces
uv run --locked python tests/l4/bench.py export-traces --abyss PATH/abyss.sqlite

General samples from aether and abyss need the local trace CSVs (export-traces); without them
only synthetic samples run. Results go to .runtime/l4bench/results/<time>/, which git ignores.
"""

import argparse
import hashlib
import json
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, get_args

from l4bench import calibrate, facts, scoring, suites, traces
from l4bench.metrics import measure
from l4kit import Scenario, run_scenario

from taskloom.policies.scheduling import PolicyName

ROOT = Path(__file__).resolve().parents[2]
# `run --policy` picks the gateway's scheduling policy (PlatformConfig.scheduling_policy).
# `run --placement` picks who chooses the upstream account: "gateway" (default) lists the pool
# accounts in the PlatformConfig so the gateway places every call; "rotate" and "sticky" list
# none, so the gateway names no account and the pool's stand-in places (the pre-WP5 setups,
# kept for comparison with earlier results).


def build(run: dict[str, Any]) -> tuple[suites.Case, suites.Probe | None]:
    if run["suite"] == "general":
        sample = suites.sample_named(run["sample"])
        return suites.general_case(sample, run["load"], run["coordinated"]), None
    probe = suites.PROBES[run["probe"]]
    return probe.build(run["seed"], run["variant"]), probe


def execute(run: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    # Worker processes build their own pools; suites.pool() reads this at call time.
    suites.PLACEMENT = run.get("placement", "rotate")
    suites.POLICY = run.get("policy", "hybrid")
    suites.OVERRIDES = run.get("overrides", {})
    case, probe = build(run)
    result = run_scenario(
        Scenario(
            case.workload, case.config, case.pool, case.coordinated, case.horizon,
            duplicate_rate=case.duplicate_rate, seed=case.seed,
        )
    )  # fmt: skip
    extra = probe.extract(result, case) if probe and probe.extract else {}
    return {
        "run": run,
        "info": {**case.info, "capacity": case.config.pool_capacity, **case.workload.meta},
        "metrics": measure(result),
        "extra": extra,
        "wall_seconds": time.monotonic() - started,
    }


def plan(
    quick: bool,
    families: list[str],
    probes: list[str],
    seeds: int,
    placement: suites.PoolPlacement = "gateway",
    policy: str = "hybrid",
    samples: int | None = None,
    loads: tuple[float, ...] | None = None,
    uncoordinated: bool = True,
    overrides: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for family in families:
        for name in suites.GENERAL[family][: 2 if quick else samples]:
            for load in loads or ((suites.FEEDBACK_LOAD,) if quick else suites.GENERAL_LOADS):
                runs.append({"suite": "general", "sample": name, "load": load, "coordinated": True})
            if uncoordinated:
                runs.append(
                    {"suite": "general", "sample": name, "load": suites.FEEDBACK_LOAD,
                     "coordinated": False}
                )  # fmt: skip
    for name in probes:
        variants = ("attack", "reference") if suites.PROBES[name].paired else ("attack",)
        for seed in range(1, (1 if quick else seeds) + 1):
            runs.extend(
                {"suite": "probe", "probe": name, "variant": v, "seed": seed} for v in variants
            )
    runs = [
        {**r, "placement": placement, "policy": policy, "overrides": overrides or {}} for r in runs
    ]
    # Longest runs first keeps the worker pool busy to the end.
    return sorted(runs, key=lambda r: r.get("probe") != "endurance")


def provenance(placement: str, policy: str) -> dict[str, Any]:
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout

    diff = git("diff", "HEAD", "--", "src")
    return {
        "benchmark_version": calibrate.VERSION,
        "policy": policy,
        "placement": placement,
        "slots_per_account": suites.SLOTS_PER_ACCOUNT,
        "commit": git("rev-parse", "HEAD").strip(),
        "src_diff_sha": hashlib.sha256(diff.encode()).hexdigest()[:12] if diff else None,
        "facts": facts.FACTS,
        "traces": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()[:12]
            for p in sorted(traces.TRACE_DIR.glob("*.csv"))
        },
    }


def run(args: argparse.Namespace) -> None:
    families = [
        f for f in args.families
        if f == "synthetic" or (traces.TRACE_DIR / f"{f}.csv").exists()
    ]  # fmt: skip
    for missing in sorted(set(args.families) - set(families)):
        print(f"skip {missing}: {traces.TRACE_DIR / (missing + '.csv')} missing")
    overrides = {k: json.loads(v) for k, v in (item.split("=", 1) for item in args.set)}
    runs = plan(
        args.quick, families, args.probes, args.seeds, args.placement, args.policy,
        args.samples, tuple(args.loads) if args.loads else None, not args.no_uncoordinated,
        overrides,
    )  # fmt: skip
    out = args.out or traces.DATA_DIR / "results" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    results = []
    with (
        ProcessPoolExecutor(max_workers=args.jobs) as executor,
        (out / "runs.jsonl").open("w") as log,
    ):
        for index, result in enumerate(executor.map(execute, runs), 1):
            log.write(json.dumps(result) + "\n")
            log.flush()
            m = result["metrics"]
            label = " ".join(
                str(v)
                for k, v in result["run"].items()
                if k not in ("placement", "policy", "overrides")
            )
            print(
                f"[{index}/{len(runs)}] {label:<44} score={scoring.run_score(m):5.1f} "
                f"ok={m['consistent']!s:<5} slo={m['slo_attainment']:.2f} "
                f"wall={result['wall_seconds']:.0f}s",
                flush=True,
            )
            results.append(result)
    summary = scoring.report(results)
    summary["provenance"] = {**provenance(args.placement, args.policy), "overrides": overrides}
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    text = scoring.render(summary)
    (out / "summary.md").write_text(text + "\n")
    print(f"\n{text}\n\n{len(runs)} runs in {time.monotonic() - started:.0f}s -> {out}")


def load(directory: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (directory / "runs.jsonl").read_text().splitlines()]


def compare(args: argparse.Namespace) -> None:
    result = scoring.paired(load(args.old), load(args.new))
    general = result["general_delta"]
    low, high = general["ci"]
    print(f"general {general['mean']:+.2f} [{low:+.2f}, {high:+.2f}] over {general['n']} runs")
    for name, delta in sorted(result["probe_delta"].items()):
        low, high = delta["ci"]
        print(f"{name:<16} {delta['mean']:+6.1f} [{low:+.1f}, {high:+.1f}]")


def rescore(args: argparse.Namespace) -> None:
    """Score a finished run's raw metrics again, e.g. after the scoring standard changed.

    Writes summary-v<VERSION>.{json,md} next to the original summary, which is kept.
    """
    summary = scoring.report(load(args.result))
    previous = json.loads((args.result / "summary.json").read_text()).get("provenance", {})
    summary["provenance"] = {**previous, "scored_by": calibrate.VERSION}
    stem = args.result / f"summary-v{calibrate.VERSION}"
    stem.with_suffix(".json").write_text(json.dumps(summary, indent=2))
    text = scoring.render(summary)
    stem.with_suffix(".md").write_text(text + "\n")
    print(text)


def check_facts(args: argparse.Namespace) -> None:
    measured = facts.measure(
        traces.load(traces.TRACE_DIR / "aether.csv"), traces.load(traces.TRACE_DIR / "abyss.csv")
    )
    for key, value in measured.items():
        drift = abs(value - facts.FACTS[key]) / max(abs(facts.FACTS[key]), 1e-9)
        print(f"{key:<24} facts={facts.FACTS[key]:<10g} measured={value:<10.4g} drift={drift:.1%}")


def export(args: argparse.Namespace) -> None:
    rows = traces.export_aether()
    traces.save(traces.TRACE_DIR / "aether.csv", rows)
    print(f"aether: {len(rows)} requests -> {traces.TRACE_DIR / 'aether.csv'}")
    if args.abyss:
        rows = traces.export_abyss(args.abyss)
        traces.save(traces.TRACE_DIR / "abyss.csv", rows)
        print(f"abyss: {len(rows)} requests -> {traces.TRACE_DIR / 'abyss.csv'}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    runner = commands.add_parser("run")
    runner.add_argument("--quick", action="store_true")
    runner.add_argument("--families", nargs="*", default=list(suites.GENERAL))
    runner.add_argument("--probes", nargs="*", default=list(suites.PROBES))
    runner.add_argument("--seeds", type=int, default=3)
    runner.add_argument("--jobs", type=int, default=8)
    runner.add_argument("--out", type=Path)
    runner.add_argument("--placement", choices=("gateway", "rotate", "sticky"), default="gateway")
    runner.add_argument("--policy", choices=get_args(PolicyName), default="hybrid")
    # Development subsets and sweeps; the reported benchmark uses the defaults.
    runner.add_argument("--samples", type=int, help="first N general samples per family")
    runner.add_argument("--loads", type=float, nargs="*", help="general loads to run")
    runner.add_argument("--no-uncoordinated", action="store_true")
    runner.add_argument(
        "--set", action="append", default=[], metavar="KEY=JSON",
        help="PlatformConfig override for every run, e.g. --set cache_wait_seconds=30",
    )  # fmt: skip
    runner.set_defaults(handler=run)
    comparer = commands.add_parser("compare")
    comparer.add_argument("old", type=Path)
    comparer.add_argument("new", type=Path)
    comparer.set_defaults(handler=compare)
    rescorer = commands.add_parser("rescore", help="re-render a result with current scoring")
    rescorer.add_argument("result", type=Path)
    rescorer.set_defaults(handler=rescore)
    commands.add_parser("facts").set_defaults(handler=check_facts)
    exporter = commands.add_parser("export-traces")
    exporter.add_argument("--abyss", type=Path)
    exporter.set_defaults(handler=export)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
