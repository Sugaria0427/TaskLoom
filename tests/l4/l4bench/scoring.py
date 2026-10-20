"""The scoring standard: hard gate, general score, probe score, and paired comparison.

Gate. A run whose ledger is inconsistent (sums disagree, a slot over capacity, a replay, a
charge that does not match one execution) scores 0, and any such run marks the whole result.

General score (0-100 per run, coordinated clients), from fixed anchors so scores stay
comparable across policies and over time:

  slo         25  per-account mean SLO credit (slowdown <= 2: 1, <= 5: 0.5, else or failed: 0)
  fairness    20  1 at 5% of busy slots misallocated vs max-min, 0 at 50%;
                  70% account level, 30% root level within accounts. The unit is pool
                  slot-time (simultaneous slots), the resource a decode-heavy request
                  actually occupies; a token-cost DRR is judged against slot-time by design,
                  and cost shares appear in the raw metrics only
  cache       35  money lost to cache misses that placement and queueing caused, as a share
                  of all charges: 1 at 0, 0 at 0.5 (half the bill paid for lost cache). Priced
                  as billed, so a missed cached token costs the write price (1.25) instead of
                  the read price (0.1), about 11x; a token-weighted hit ratio would hide that.
                  Accounting has no separate part: an over-spent upstream account stops, its
                  chains move to another account and lose their cache, and that is charged
                  here. Reservation precision and overrun remain raw metrics
  starvation  10  1 with no request waiting past 300 s, 0 at 10% of them. 300 s is where a
                  prompt cache starts to fade and an interactive user has long noticed, not a
                  cache lifetime
  efficiency  10  1 at no wanted slot-time idle, 0 at 20% idle

General score = mean of family means (aether, abyss, synthetic weigh equally whatever their
sample counts); a run's score is first averaged over its loads per sample. Its 95% interval
is a bootstrap that resamples samples within each family.

Probe score (0-100 each): the probe's own judge, mean over seeds; >= 80 passes. A paired
probe missing its reference run scores 0 for that seed. Seeds are few, so probe intervals
are Student-t, not bootstrap.

Total = 0.6 general + 0.4 mean probe score. Feedback gain is reported, not scored: the
paired SLO difference between coordinated and uncoordinated clients on the same samples.
Results are also broken down by nominal load with the realized load range, since capacity
rounding moves realized load between samples.
"""

import math
import random
import statistics
from collections import defaultdict
from typing import Any

from .suites import FEEDBACK_LOAD, PROBES

WEIGHTS = {"slo": 25, "fairness": 20, "cache": 35, "starvation": 10, "efficiency": 10}
PASS = 0.8
GENERAL_SHARE = 0.6


def _linear(value: float, good: float, bad: float) -> float:
    return max(0.0, min(1.0, (bad - value) / (bad - good)))


def subscores(m: dict[str, Any]) -> dict[str, float]:
    fairness = 0.7 * _linear(m["account_unfairness"], 0.05, 0.5) + 0.3 * _linear(
        m["root_unfairness"], 0.05, 0.5
    )
    return {
        "slo": m["slo_attainment"],
        "fairness": fairness,
        "cache": _linear(m["cache_miss_cost"], 0.0, 0.5),
        "starvation": _linear(m["starved"], 0.0, 0.10),
        "efficiency": _linear(m["waste"], 0.0, 0.20),
    }


def run_score(m: dict[str, Any]) -> float:
    if not m["consistent"]:
        return 0.0
    parts = subscores(m)
    return sum(WEIGHTS[k] * parts[k] for k in WEIGHTS)


T95 = {1: 12.71, 2: 4.30, 3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45, 7: 2.36, 8: 2.31, 9: 2.26}


def family(sample: str) -> str:
    return sample.rsplit("-", 1)[0]


def bootstrap(values: list[float], rounds: int = 4000, seed: int = 0) -> tuple[float, float]:
    if len(values) < 2:
        return (values[0], values[0]) if values else (0.0, 0.0)
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(values, k=len(values))) for _ in range(rounds))
    return means[int(0.025 * rounds)], means[int(0.975 * rounds) - 1]


def stratified_bootstrap(
    groups: dict[str, list[float]], rounds: int = 4000, seed: int = 0
) -> tuple[float, float]:
    """Interval of the mean of group means, resampling within each group."""
    groups = {k: v for k, v in groups.items() if v}
    if not groups:
        return 0.0, 0.0
    rng = random.Random(seed)
    means = sorted(
        statistics.fmean(statistics.fmean(rng.choices(v, k=len(v))) for v in groups.values())
        for _ in range(rounds)
    )
    return means[int(0.025 * rounds)], means[int(0.975 * rounds) - 1]


def t_interval(values: list[float]) -> tuple[float, float]:
    """Student-t 95% interval of the mean, for a handful of seeds."""
    if len(values) < 2:
        return (values[0], values[0]) if values else (0.0, 0.0)
    mean = statistics.fmean(values)
    half = T95.get(len(values) - 1, 1.96) * statistics.stdev(values) / math.sqrt(len(values))
    return mean - half, mean + half


def _summary(values: list[float]) -> dict[str, Any]:
    low, high = bootstrap(values)
    return {
        "mean": statistics.fmean(values) if values else 0.0,
        "ci": [low, high],
        "n": len(values),
    }


def _t_summary(values: list[float], bounds: tuple[float, float]) -> dict[str, Any]:
    # With three seeds the t interval can run past what a score can be; clip it to that range.
    low, high = t_interval(values)
    return {
        "mean": statistics.fmean(values) if values else 0.0,
        "ci": [max(bounds[0], low), min(bounds[1], high)],
        "n": len(values),
    }


def _family_summary(per_sample: dict[str, float]) -> dict[str, Any]:
    """Families weigh equally: the mean of family means, with a within-family bootstrap."""
    groups: dict[str, list[float]] = defaultdict(list)
    for sample, value in per_sample.items():
        groups[family(sample)].append(value)
    low, high = stratified_bootstrap(groups)
    return {
        "mean": statistics.fmean(statistics.fmean(v) for v in groups.values()) if groups else 0.0,
        "ci": [low, high],
        "n": len(per_sample),
    }


def general_rows(results: list[dict[str, Any]], coordinated: bool = True) -> list[dict[str, Any]]:
    return [
        r for r in results
        if r["run"]["suite"] == "general" and r["run"]["coordinated"] == coordinated
    ]  # fmt: skip


def probe_scores(results: list[dict[str, Any]]) -> tuple[dict[str, list[float]], list[str]]:
    """Judge scores per probe over seeds, and the probes missing a paired reference run.

    A paired probe without its reference scores 0 for that seed rather than being dropped,
    so a crashed or skipped reference can never raise the result.
    """
    runs: dict[tuple[str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for result in results:
        run = result["run"]
        if run["suite"] == "probe":
            runs[(run["probe"], run["seed"])][run["variant"]] = {
                **result["metrics"],
                **result["extra"],
            }
    scores: dict[str, list[float]] = defaultdict(list)
    missing: set[str] = set()
    for (name, _), variants in sorted(runs.items()):
        attack = variants.get("attack")
        if attack is None:
            continue
        reference = variants.get("reference")
        if PROBES[name].paired and reference is None:
            missing.add(name)
            scores[name].append(0.0)
            continue
        gate = attack["consistent"] and (reference or attack)["consistent"]
        scores[name].append(PROBES[name].judge(attack, reference) if gate else 0.0)
    return scores, sorted(missing)


RAW = (
    "slo_attainment", "account_unfairness", "root_unfairness", "account_shortfall",
    "root_shortfall", "cache_retention",
    "cache_miss_cost", "account_moves", "starved", "waste", "reservation_precision", "overrun",
    "slowdown_p50", "slowdown_p95", "wait_p95", "completed",
)  # fmt: skip


def _per_sample(rows: list[dict[str, Any]], value: Any) -> dict[str, float]:
    collected: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        collected[row["run"]["sample"]].append(float(value(row["metrics"])))
    return {k: statistics.fmean(v) for k, v in collected.items()}


def report(results: list[dict[str, Any]]) -> dict[str, Any]:
    inconsistent = [r["run"] for r in results if not r["metrics"]["consistent"]]
    general = general_rows(results)
    sample_scores = _per_sample(general, run_score)

    families: dict[str, list[float]] = defaultdict(list)
    for sample, score in sample_scores.items():
        families[family(sample)].append(score)

    by_load: dict[str, dict[str, Any]] = {}
    for nominal in sorted({row["run"]["load"] for row in general}):
        rows = [row for row in general if row["run"]["load"] == nominal]
        realized = [float(row["info"]["load"]) for row in rows if "load" in row.get("info", {})]
        by_load[str(nominal)] = {
            **_family_summary(_per_sample(rows, run_score)),
            "realized": {
                "mean": statistics.fmean(realized) if realized else None,
                "min": min(realized) if realized else None,
                "max": max(realized) if realized else None,
            },
        }

    # Feedback gain: same sample and load, coordinated minus uncoordinated.
    pairs: dict[str, dict[bool, float]] = defaultdict(dict)
    for row in results:
        run = row["run"]
        if run["suite"] == "general" and run["load"] == FEEDBACK_LOAD:
            pairs[run["sample"]][run["coordinated"]] = row["metrics"]["slo_attainment"]
    gains = [v[True] - v[False] for v in pairs.values() if len(v) == 2]

    probes, missing = probe_scores(results)
    probe_table = {
        name: {
            **_t_summary([100 * s for s in values], (0.0, 100.0)),
            "min": 100 * min(values),
            "pass": statistics.fmean(values) >= PASS,
            "fact": PROBES[name].fact,
            "claim": PROBES[name].claim,
        }
        for name, values in probes.items()
    }
    general_summary = _family_summary(sample_scores)
    probe_score = (
        statistics.fmean(row["mean"] for row in probe_table.values()) if probe_table else 0.0
    )
    return {
        "gate": {"passed": not inconsistent, "inconsistent_runs": inconsistent[:10]},
        "total": GENERAL_SHARE * general_summary["mean"] + (1 - GENERAL_SHARE) * probe_score,
        "general": {
            **general_summary,
            "parts": {
                k: _family_summary(_per_sample(general, lambda m, k=k: 100 * subscores(m)[k]))
                for k in WEIGHTS
            },
            "by_family": {k: _summary(v) for k, v in families.items()},
            "by_load": by_load,
            "raw": {
                k: _family_summary(_per_sample(general, lambda m, k=k: m.get(k, math.nan)))
                for k in RAW
            },
        },
        "probes": {"mean": probe_score, "table": probe_table, "missing_reference": missing},
        "feedback_gain_slo": _summary(gains),
    }


def paired(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> dict[str, Any]:
    """Score differences b - a on identical general runs and probe seeds."""

    def keyed(results: list[dict[str, Any]]) -> dict[tuple[Any, ...], float]:
        return {
            (r["run"]["sample"], r["run"]["load"]): run_score(r["metrics"])
            for r in general_rows(results)
        }

    left, right = keyed(a), keyed(b)
    general = [right[k] - left[k] for k in left.keys() & right.keys()]
    probes_a, probes_b = probe_scores(a)[0], probe_scores(b)[0]
    return {
        "general_delta": _summary(general),
        "probe_delta": {
            name: _t_summary(
                [100 * (y - x) for x, y in zip(probes_a[name], probes_b[name], strict=False)],
                (-100.0, 100.0),
            )
            for name in probes_a.keys() & probes_b.keys()
        },
    }


def render(summary: dict[str, Any]) -> str:
    def fmt(cell: dict[str, Any], digits: int = 1) -> str:
        low, high = cell["ci"]
        return f"{cell['mean']:.{digits}f} [{low:.{digits}f}, {high:.{digits}f}]"

    general = summary["general"]
    lines = [
        f"**Total {summary['total']:.1f}** = 0.6 x general {general['mean']:.1f} "
        f"+ 0.4 x probes {summary['probes']['mean']:.1f}",
        "",
        "Gate: " + ("passed" if summary["gate"]["passed"]
                    else f"FAILED, inconsistent runs: {summary['gate']['inconsistent_runs']}"),
        "",
        f"## General ({general['n']} samples, families weighted equally): {fmt(general)}",
        "",
        "| part | weight | score |",
        "|---|---|---|",
        *(f"| {k} | {WEIGHTS[k]} | {fmt(general['parts'][k])} |" for k in WEIGHTS),
        "",
        "| family | samples | score |",
        "|---|---|---|",
    ]  # fmt: skip
    for name, cell in sorted(general["by_family"].items()):
        lines.append(f"| {name} | {cell['n']} | {fmt(cell)} |")
    lines += [
        "",
        "| nominal load | score | realized load mean [min, max] |",
        "|---|---|---|",
    ]
    for nominal, cell in sorted(general["by_load"].items()):
        realized = cell["realized"]
        span = (
            f"{realized['mean']:.2f} [{realized['min']:.2f}, {realized['max']:.2f}]"
            if realized["mean"] is not None
            else "n/a"
        )
        lines.append(f"| {nominal} | {fmt(cell)} | {span} |")
    lines += ["", "| raw metric | mean [95% CI] |", "|---|---|"]
    lines += [f"| {k} | {fmt(v, 3)} |" for k, v in general["raw"].items()]
    gain = summary["feedback_gain_slo"]
    lines += [
        "",
        f"Feedback gain (coordinated - uncoordinated SLO, load {FEEDBACK_LOAD}, "
        f"{gain['n']} samples): {fmt(gain, 3)}",
        "",
        f"## Probes: {summary['probes']['mean']:.1f}",
        "",
        *(
            [f"Missing reference runs (scored 0): {summary['probes']['missing_reference']}", ""]
            if summary["probes"].get("missing_reference")
            else []
        ),
        "| probe | score | min | pass | fact | claim |",
        "|---|---|---|---|---|---|",
    ]
    for name, row in sorted(summary["probes"]["table"].items()):
        lines.append(
            f"| {name} | {fmt(row)} | {row['min']:.0f} | {'yes' if row['pass'] else 'no'} "
            f"| {row['fact']} | {row['claim']} |"
        )
    return "\n".join(lines)
