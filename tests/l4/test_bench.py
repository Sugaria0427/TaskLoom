"""The benchmark's own correctness: fairness reference, metric sanity, scoring, determinism."""

import pytest
from l4bench import calibrate as cal
from l4bench import facts, scoring, suites, traces
from l4bench.metrics import measure, slo_credit
from l4bench.timeline import _deviation, waterfill
from l4kit import Behavior, Scenario, ScriptedPool, iso, run_scenario, run_virtual, sized_messages
from l4kit.pool import Placement

from taskloom.contracts.models import ModelRequest


def test_waterfill_gives_small_demands_all_and_splits_the_rest_evenly() -> None:
    assert waterfill({"a": 1, "b": 10, "c": 10}, 7) == {"a": 1.0, "b": 3.0, "c": 3.0}
    assert waterfill({"a": 2, "b": 2}, 8) == {"a": 2.0, "b": 2.0}


def test_deviation_counts_misallocated_slots() -> None:
    # One chain each: holding one slot apiece is fair even if a third party waits for more.
    assert _deviation({"a": 1, "b": 5}, {"a": 1, "b": 1}) == 0.0
    # Four slots in use, fair split 2/2, held 4/0: half the slots sit with the wrong party.
    assert _deviation({"a": 4, "b": 4}, {"a": 4}) == 0.5


def test_slo_credit_tiers() -> None:
    assert [slo_credit(v) for v in (1.0, 2.0, 3.0, 5.0, 9.0, None)] == [1, 1, 0.5, 0.5, 0, 0]


def _uncontended() -> dict:
    case = suites.fanout_agents(1, "attack")
    capacity = 64
    result = run_scenario(
        Scenario(
            case.workload, suites.platform(capacity), suites.pool(capacity),
            horizon_seconds=case.horizon,
        )
    )  # fmt: skip
    return measure(result)


def test_an_idle_platform_scores_as_ideal() -> None:
    m = _uncontended()
    assert m["consistent"]
    assert m["account_unfairness"] == 0.0 and m["contended_seconds"] < 1.0
    assert m["slo_attainment"] == 1.0 and m["starved"] == 0.0
    # Only the gateway's 0.1 s dispatch poll separates ready from running.
    assert m["waste"] < 0.02


def test_runs_are_deterministic() -> None:
    def once() -> dict:
        case = suites.decode_heavy(2, "attack")
        result = run_scenario(
            Scenario(case.workload, case.config, case.pool, horizon_seconds=case.horizon)
        )
        m = measure(result)
        return {k: v for k, v in m.items() if k != "virtual_seconds"}

    assert once() == once()


def test_gate_zeroes_inconsistent_runs_and_anchors_are_monotone() -> None:
    m = _uncontended()
    assert scoring.run_score(m) > 0
    assert scoring.run_score({**m, "consistent": False}) == 0
    worse = {**m, "account_unfairness": 0.3, "starved": 0.05, "waste": 0.1}
    assert scoring.run_score(worse) < scoring.run_score(m)


@pytest.mark.parametrize(("policy", "passes"), [("rr", False), ("hybrid", True)])
def test_decode_heavy_users_hold_the_pool_under_rr_but_not_hybrid(
    policy: str, passes: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rr shares dispatches, so long decodes keep slots while others wait; hybrid gives a
    free slot to whoever holds the fewest. The attacker's total share barely changes: what
    differs is who holds the slots while someone is waiting."""
    monkeypatch.setattr(suites, "POLICY", policy)
    probe = suites.PROBES["decode-heavy"]
    case = probe.build(1, "attack")
    result = run_scenario(
        Scenario(case.workload, case.config, case.pool, horizon_seconds=case.horizon)
    )
    attack = {**measure(result), **(probe.extract(result, case) if probe.extract else {})}
    assert attack["consistent"]
    assert attack["attacker_share"] > 0.5  # four accounts, one of them holds most slot time
    assert (probe.judge(attack, None) >= scoring.PASS) is passes


@pytest.mark.skipif(
    not (traces.TRACE_DIR / "aether.csv").exists() or not (traces.TRACE_DIR / "abyss.csv").exists(),
    reason="private traces are local only",
)
def test_facts_match_the_local_traces() -> None:
    measured = facts.measure(
        traces.load(traces.TRACE_DIR / "aether.csv"), traces.load(traces.TRACE_DIR / "abyss.csv")
    )
    for key, value in measured.items():
        assert value == pytest.approx(facts.FACTS[key], rel=0.03), key


def test_synthetic_steps_follow_the_measured_distributions() -> None:
    import random

    rng = random.Random(0)
    steps = [cal.sample_step(rng) for _ in range(4000)]
    prompts = sorted(s.input_tokens * cal.SCALE for s in steps)
    outputs = sorted(s.output_tokens * cal.SCALE for s in steps)
    assert prompts[2000] == pytest.approx(facts.FACTS["input_p50"], rel=0.1)
    assert prompts[3600] == pytest.approx(facts.FACTS["input_p90"], rel=0.1)
    # Outputs are stored at SCALE resolution, so only the tail is checked tightly.
    assert outputs[3600] == pytest.approx(facts.FACTS["output_p90"], rel=0.15)


def test_cache_retention_fades_from_warm_to_cold() -> None:
    assert facts.cache_retention(0.0) == facts.cache_retention(20.0) == 1.0
    idles = [10.0 * 1.5**i for i in range(30)]
    values = [facts.cache_retention(t) for t in idles]
    assert all(a >= b for a, b in zip(values, values[1:], strict=False))
    assert facts.cache_retention(None) == pytest.approx(0.116 / 0.928)


def test_split_spreads_slots_over_at_least_two_accounts() -> None:
    assert suites.split(2) == (1, 1)
    assert suites.split(3) == (2, 1)
    assert suites.split(8) == (2, 2, 2, 2)
    assert all(sum(suites.split(n)) == n for n in range(2, 65))


def _pool_extras(placement: Placement) -> list[tuple[bool, int]]:
    """Three sequential calls of one chain on two one-slot accounts: (moved, extra_units)."""
    pool = ScriptedPool(accounts=(1, 1), placement=placement, retention=facts.cache_retention)

    async def main() -> None:
        for r in range(3):
            name = f"root-b0-r{r}"
            pool.plan(name, Behavior(cost_units=100, cached_tokens=1000, cache_discount=1.15))
            await pool.generate(
                ModelRequest(
                    request_id=name, root_task_id="root", agent_id="root-b0", step_id=name,
                    attempt_id=name, messages=sized_messages(200), deadline_at=iso(4e9),
                )
            )  # fmt: skip

    run_virtual(main)
    return [(call.moved, call.extra_units) for call in pool.log]


def test_a_chain_pays_for_its_cache_only_when_it_moves_account() -> None:
    assert _pool_extras("sticky") == [(False, 0)] * 3
    # Round-robin: the second call lands on an account it never used and rewrites 7/8 of
    # its cache; the third returns to the first account while it is still warm.
    assert _pool_extras("rotate") == [(False, 0), (True, round(1000 * 0.875 * 1.15)), (True, 0)]


def test_cache_affinity_rewards_keeping_chains_on_their_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = suites.PROBES["cache-affinity"]

    def judged(placement: suites.PoolPlacement) -> float:
        monkeypatch.setattr(suites, "PLACEMENT", placement)
        case = probe.build(1, "attack")
        result = run_scenario(
            Scenario(case.workload, case.config, case.pool, horizon_seconds=case.horizon)
        )
        assert probe.extract is not None
        return probe.judge(probe.extract(result, case), None)

    sticky, rotate = judged("sticky"), judged("rotate")
    assert sticky >= 0.8
    assert rotate < sticky - 0.25
    # The gateway's own placement (pool accounts in the config) keeps the cache like sticky.
    assert judged("gateway") >= 0.8
