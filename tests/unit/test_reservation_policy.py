from taskloom.policies.reservation import Prices, ReservationEstimator, worst_units

PRICED = Prices(output=5.0, cache_read=0.1, cache_write=1.25)


def test_worst_case_bills_misses_as_writes_and_full_output() -> None:
    assert worst_units(1000, 200, PRICED) == 1250 + 1000
    assert worst_units(1000, 200, Prices()) == 1200


def test_estimate_improves_with_chain_history() -> None:
    estimator = ReservationEstimator(PRICED)
    # No history: nothing cached and the full max_tokens, capped at the worst case.
    first = estimator.estimate("alice", "agent", 1000, 200, now=0.0)
    assert (first.units, first.tokens) == (1000 + 1000, 1200)
    estimator.dispatched("r0", "alice", "agent", 1000, now=0.0)
    estimator.settled("r0", "alice", "agent", 1000, 10, cost_units=1050, now=5.0)
    # Warm chain whose prompt grew by 100: 1000 of 1100 tokens cached, ~15 output tokens.
    warm = estimator.estimate("alice", "agent", 1100, 200, now=30.0)
    assert warm.units == round(100 + 1000 * 0.1 + 15 * 5.0)
    assert warm.tokens == 1100 + 15
    actual = 100 + 1000 * 0.1 + 12 * 5.0
    assert abs(warm.units - actual) < abs(first.units - actual) / 10
    # Other chains of the account inherit its output history but not its cache.
    other = estimator.estimate("alice", "other", 1100, 200, now=30.0)
    assert other.units == 1100 + 15 * 5
    # An hour after the chain last finished, the cache is assumed gone.
    cold = estimator.estimate("alice", "agent", 1100, 200, now=3700.0)
    assert cold.units == 1100 + 15 * 5


def test_realized_cache_hits_cap_the_expected_cached_share() -> None:
    estimator = ReservationEstimator(PRICED)
    estimator.dispatched("r0", "alice", "agent", 1000, now=0.0)
    estimator.settled("r0", "alice", "agent", 1000, 10, cost_units=1050, now=5.0)
    estimator.dispatched("r1", "alice", "agent", 1000, now=10.0)
    # The chain moved to an upstream that kept half the prompt: 500 + 50 + 50 charged.
    estimator.settled("r1", "alice", "agent", 1000, 10, cost_units=600, now=15.0)
    assert estimator.cached_share("alice", "agent", 1000, now=20.0) == 0.5


def test_estimator_state_is_bounded() -> None:
    estimator = ReservationEstimator(Prices())
    for index in range(ReservationEstimator.MAX_ENTRIES + 10):
        estimator.dispatched(f"r{index}", "alice", f"agent{index}", 10, now=0.0)
    assert len(estimator._chains) == len(estimator._warm) == ReservationEstimator.MAX_ENTRIES
