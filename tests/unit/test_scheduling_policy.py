import random
from collections import Counter
from collections.abc import Callable

import pytest
from pydantic import ValidationError

from taskloom.contracts.models import PlatformConfig, PoolAccount
from taskloom.policies.fair import FairQueue
from taskloom.policies.placement import AffinityPlacement
from taskloom.policies.scheduling import (
    HybridPolicy,
    MaxMinPolicy,
    OccupancyDrrPolicy,
    RoundRobinPolicy,
    SchedulingPolicy,
    scheduling_policy,
)


class LegacyGatewayQueue:
    """The gateway's pre-WP5 inline tenant/root round-robin, kept as the equivalence oracle."""

    def __init__(self) -> None:
        self.queue: FairQueue[str] = FairQueue()
        self.roots: dict[str, FairQueue[str]] = {}

    def push(self, tenant: str, root: str, key: str) -> None:
        if tenant not in self.roots:
            self.roots[tenant] = FairQueue()
            self.queue.push(tenant, tenant)
        self.roots[tenant].push(root, key)

    def pick(self, feasible: Callable[[str], bool]) -> str | None:
        for _ in range(len(self.roots)):
            tenant = self.queue.pop()
            if tenant is None:
                return None
            inner = self.roots[tenant]
            key = inner.pop(feasible)
            if len(inner):
                self.queue.push(tenant, tenant)
            else:
                del self.roots[tenant]
            if key is not None:
                return key
        return None


@pytest.mark.parametrize("seed", range(5))
def test_round_robin_policy_matches_the_legacy_gateway_queue(seed: int) -> None:
    rng = random.Random(seed)
    policy, legacy = RoundRobinPolicy(), LegacyGatewayQueue()
    blocked: set[str] = set()
    for step in range(400):
        if rng.random() < 0.55:
            tenant = f"t{rng.randrange(4)}"
            root = f"{tenant}-r{rng.randrange(3)}"
            key = f"k{step}"
            policy.push(tenant, root, key, float(step))
            legacy.push(tenant, root, key)
            if rng.random() < 0.2:
                blocked.add(key)
        else:
            if rng.random() < 0.3 and blocked:
                blocked.discard(rng.choice(sorted(blocked)))
            assert policy.pick(lambda k: k not in blocked, float(step)) == legacy.pick(
                lambda k: k not in blocked
            )
    assert len(policy) == sum(len(inner) for inner in legacy.roots.values())


def simulate(
    policy: SchedulingPolicy, durations: dict[str, float], slots: int, horizon: float
) -> Counter[str]:
    """Every tenant stays backlogged; returns slot-seconds held per tenant up to the horizon."""
    now, held = 0.0, Counter[str]()
    running: list[tuple[float, str, str]] = []  # (finish, key, tenant)
    serial = 0

    def refill(tenant: str) -> None:
        nonlocal serial
        policy.push(tenant, tenant + "-root", f"{tenant}-{serial}", now)
        serial += 1

    for tenant in durations:
        for _ in range(slots):
            refill(tenant)
    while now < horizon:
        while len(running) < slots:
            key = policy.pick(lambda _: True, now)
            assert key is not None
            tenant = key.rsplit("-", 1)[0]
            policy.on_dispatch(key, tenant, tenant + "-root", now)
            running.append((now + durations[tenant], key, tenant))
            refill(tenant)
        running.sort()
        finish, key, tenant = running.pop(0)
        start = finish - durations[tenant]
        held[tenant] += min(finish, horizon) - start
        now = finish
        policy.on_finish(key, now, 0)
    for finish, _, tenant in running:
        held[tenant] += max(0.0, horizon - (finish - durations[tenant]))
    return held


def test_occupancy_policy_gives_decode_heavy_tenant_no_extra_slot_time() -> None:
    durations = {"heavy": 10.0, "light": 1.0}
    rr = simulate(RoundRobinPolicy(), durations, slots=2, horizon=400)
    drr = simulate(OccupancyDrrPolicy(), durations, slots=2, horizon=400)
    assert rr["heavy"] > 5 * rr["light"]  # equal dispatches, unequal occupancy
    assert drr["heavy"] <= drr["light"] * 1.1
    assert drr["light"] <= drr["heavy"] * 1.25  # light is not starving heavy either


def test_occupancy_policy_fanout_within_a_root_or_tenant_earns_no_share() -> None:
    policy = OccupancyDrrPolicy()
    for index in range(6):
        policy.push("wide", f"wide-r{index}", f"wide-{index}", 0.0)  # six roots
        policy.push("narrow", "narrow-r0", f"narrow-{index}", 0.0)  # one root
    picks = []
    for _ in range(6):
        key = policy.pick(lambda _: True, 0.0)
        assert key is not None
        tenant, index = key.split("-")
        policy.on_dispatch(key, tenant, f"{tenant}-r{index if tenant == 'wide' else 0}", 0.0)
        picks.append(tenant)
    assert Counter(picks) == {"wide": 3, "narrow": 3}
    # Inside one tenant, a root's fan-out competes as one flow against a single-agent root.
    inner = OccupancyDrrPolicy()
    for index in range(4):
        inner.push("user", "fanout", f"f{index}", 0.0)
    inner.push("user", "solo", "s0", 0.0)
    inner.push("user", "solo", "s1", 0.0)
    order = []
    for _ in range(4):
        key = inner.pick(lambda _: True, 0.0)
        assert key is not None
        inner.on_dispatch(key, "user", "fanout" if key[0] == "f" else "solo", 0.0)
        order.append(key[0])
    assert Counter(order) == {"f": 2, "s": 2}


def test_occupancy_policy_banks_no_idle_credit() -> None:
    policy = OccupancyDrrPolicy()
    # "early" holds the pool alone for 100 slot-seconds before "late" shows up.
    policy.push("early", "e", "e0", 0.0)
    assert policy.pick(lambda _: True, 0.0) == "e0"
    policy.on_dispatch("e0", "early", "e", 0.0)
    policy.on_finish("e0", 100.0, 0)
    for index in range(1, 4):
        policy.push("early", "e", f"e{index}", 100.0)
        policy.push("late", "l", f"l{index}", 100.0)
    order = []
    for _ in range(6):
        key = policy.pick(lambda _: True, 100.0)
        assert key is not None
        order.append(key[0])
        policy.on_dispatch(key, "early" if key[0] == "e" else "late", key[0], 100.0)
    # Without the lift "late" would take every slot until it had used 100 slot-seconds.
    assert order.count("e") == 3
    assert order[:2] != ["l", "l"] or order[2] == "e"


def test_occupancy_policy_bypasses_infeasible_work_and_skips_unknown_finishes() -> None:
    policy = OccupancyDrrPolicy()
    policy.push("a", "r", "a0", 0.0)
    policy.push("b", "r", "b0", 0.0)
    assert policy.pick(lambda key: key != "a0", 0.0) == "b0"
    assert policy.pick(lambda _: False, 0.0) is None
    policy.on_finish("never-dispatched", 5.0, 10)
    assert len(policy) == 1
    assert policy.pick(lambda _: True, 0.0) == "a0"


def test_policy_factory_names() -> None:
    assert scheduling_policy("rr").name == "rr"
    assert scheduling_policy("drr").name == "drr"
    assert scheduling_policy("maxmin").name == "maxmin"
    assert scheduling_policy("hybrid").name == "hybrid"


def test_placement_keeps_a_chain_on_its_warm_account_and_falls_back_to_least_loaded() -> None:
    placement = AffinityPlacement({"a0": 2, "a1": 2})
    assert placement.choose("chain", {}, 0.0) == "a0"
    # Warm and free: stays even though a1 is idle.
    assert placement.choose("chain", {"a0": 1}, 1.0) == "a0"
    # Warm account full: least loaded free account, which becomes the chain's new home.
    assert placement.choose("chain", {"a0": 2, "a1": 1}, 2.0) == "a1"
    assert placement.choose("chain", {"a0": 0, "a1": 1}, 3.0) == "a1"
    assert placement.choose("other", {"a0": 1, "a1": 0}, 4.0) == "a1"
    assert placement.choose("chain", {"a0": 2, "a1": 2}, 5.0) is None
    assert not placement.has_free({"a0": 2, "a1": 2})


def test_placement_returns_to_an_account_the_chain_used_before_rather_than_a_cold_one() -> None:
    placement = AffinityPlacement({"a0": 2, "a1": 2, "a2": 2})
    assert placement.choose("chain", {}, 0.0) == "a0"
    assert placement.choose("chain", {"a0": 2}, 1.0) == "a1"
    # a1 is full; a0 still holds part of the cache while a2 (idler) holds none.
    assert placement.choose("chain", {"a0": 1, "a1": 2}, 2.0) == "a0"
    # Both used and free: the more recent one.
    assert placement.choose("chain", {}, 3.0) == "a0"


def test_delay_scheduling_holds_a_request_back_for_its_full_account_for_a_bounded_time() -> None:
    placement = AffinityPlacement({"a0": 1, "a1": 1}, wait_seconds=10.0)
    assert placement.ready("new", {"a0": 1}, 0.0)  # no cache anywhere: nothing to wait for
    assert placement.choose("chain", {}, 0.0) == "a0"
    assert placement.ready("chain", {}, 1.0)
    assert not placement.ready("chain", {"a0": 1}, 1.0)
    assert not placement.ready("chain", {"a0": 1}, 10.9)
    assert placement.ready("chain", {"a0": 1}, 11.0)
    assert placement.choose("chain", {"a0": 1}, 11.0) == "a1"
    # Placing it clears the wait; the next time a1 is full it waits afresh.
    assert not placement.ready("chain", {"a1": 1}, 12.0)
    assert AffinityPlacement({"a0": 1}).ready("chain", {"a0": 1}, 0.0)


def _drive(policy: SchedulingPolicy, held: dict[str, int], queued: dict[str, int]) -> str | None:
    """Queue `queued[t]` requests per tenant after `held[t]` running ones; return who goes next."""
    for tenant, count in held.items():
        for index in range(count):
            key = f"{tenant}-run{index}"
            policy.push(tenant, tenant + "-root", key, 0.0)
            assert policy.pick(lambda k, key=key: k == key, 0.0) == key
            policy.on_dispatch(key, tenant, tenant + "-root", 0.0)
    for tenant, count in queued.items():
        for index in range(count):
            policy.push(tenant, tenant + "-root", f"{tenant}-q{index}", 1.0)
    picked = policy.pick(lambda _: True, 1.0)
    return None if picked is None else picked.split("-")[0]


def test_maxmin_gives_the_slot_to_the_backlogged_tenant_holding_the_fewest() -> None:
    # Round-robin turns ignore what each tenant already holds; max-min does not.
    assert _drive(MaxMinPolicy(), {"heavy": 5, "light": 1}, {"heavy": 3, "light": 1}) == "light"
    assert _drive(MaxMinPolicy(), {"heavy": 5}, {"heavy": 1, "light": 1}) == "light"
    assert _drive(MaxMinPolicy(), {"light": 1}, {"heavy": 3}) == "heavy"


def test_maxmin_is_round_robin_among_equals_and_forgets_idle_parties() -> None:
    policy = MaxMinPolicy()
    for tenant in ("a", "b", "c"):
        for index in range(2):
            policy.push(tenant, tenant, f"{tenant}{index}", 0.0)
    order = [policy.pick(lambda _: True, 0.0) for _ in range(6)]
    assert order == ["a0", "b0", "c0", "a1", "b1", "c1"]
    assert len(policy) == 0 and policy.held("a") == 0
    policy.push("a", "a", "a2", 0.0)
    policy.on_dispatch(policy.pick(lambda _: True, 0.0) or "", "a", "a", 0.0)
    assert policy.held("a") == 1
    policy.on_finish("a2", 1.0, 0)
    assert policy.held("a") == 0 and not policy._tenants


def test_maxmin_balances_roots_inside_a_tenant_and_bypasses_infeasible_heads() -> None:
    policy = MaxMinPolicy()
    for index in range(3):
        policy.push("user", "wide", f"w{index}", 0.0)
        policy.on_dispatch(
            policy.pick(lambda k, i=index: k == f"w{i}", 0.0) or "", "user", "wide", 0.0
        )
    policy.push("user", "wide", "w3", 1.0)
    policy.push("user", "narrow", "n0", 1.0)
    policy.push("user", "narrow", "n1", 1.0)
    assert policy.pick(lambda _: True, 1.0) == "n0"
    assert policy.pick(lambda k: k != "n1", 1.0) == "w3"


def test_single_account_config_is_unchanged_and_accounts_define_capacity() -> None:
    legacy = PlatformConfig(pool_capacity=3)
    assert legacy.pool_accounts == [] and legacy.pool_capacity == 3
    assert legacy.scheduling_policy == "rr"
    pooled = PlatformConfig(
        pool_accounts=[PoolAccount(account_id="a0", capacity=2), {"account_id": "a1"}]
    )
    assert pooled.pool_capacity == 4
    assert PlatformConfig.model_validate(pooled.model_dump()).pool_capacity == 4
    assert "secret" not in repr(PoolAccount(account_id="a0", key="secret"))
    with pytest.raises(ValidationError, match="sum"):
        PlatformConfig(pool_capacity=3, pool_accounts=[{"account_id": "a0", "capacity": 2}])
    with pytest.raises(ValidationError, match="unique"):
        PlatformConfig(pool_accounts=[{"account_id": "a0"}, {"account_id": "a0"}])
    with pytest.raises(ValidationError):
        PlatformConfig(scheduling_policy="fifo")


def test_hybrid_ranks_by_slots_held_then_by_recent_slot_time() -> None:
    assert _drive(HybridPolicy(), {"heavy": 5, "light": 1}, {"heavy": 3, "light": 1}) == "light"
    policy = HybridPolicy()
    # Equal slots held (none); the tenant that used 100 slot-seconds waits for the other.
    policy.push("used", "u", "u0", 0.0)
    policy.on_dispatch(policy.pick(lambda _: True, 0.0) or "", "used", "u", 0.0)
    policy.push("fresh", "f", "f0", 0.0)
    policy.on_dispatch(policy.pick(lambda _: True, 0.0) or "", "fresh", "f", 0.0)
    # Both stay backlogged, so neither is lifted on return.
    policy.push("used", "u", "u1", 0.5)
    policy.push("fresh", "f", "f1", 0.5)
    policy.on_finish("f0", 1.0, 0)
    policy.on_finish("u0", 100.0, 0)
    assert policy.pick(lambda _: True, 100.0) == "f1"


@pytest.mark.parametrize("name", ["rr", "drr", "maxmin", "hybrid"])
def test_discard_drops_a_queued_key_and_keeps_the_rest_in_order(name: str) -> None:
    policy = scheduling_policy(name)  # type: ignore[arg-type]
    for key in ("a1", "a2", "a3"):
        policy.push("alice", "root", key, 0.0)
    policy.push("bob", "solo", "b1", 0.0)
    policy.discard("a2")
    policy.discard("b1")
    policy.discard("missing")
    assert len(policy) == 2
    picked = [policy.pick(lambda _key: True, 1.0), policy.pick(lambda _key: True, 1.0)]
    assert picked == ["a1", "a3"]
    assert policy.pick(lambda _key: True, 1.0) is None and len(policy) == 0
    # A party emptied by discards takes new work as usual.
    policy.push("bob", "solo", "b2", 2.0)
    assert policy.pick(lambda _key: True, 2.0) == "b2"
