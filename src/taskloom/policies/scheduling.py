"""Platform L4 scheduling policies: which queued model request takes the next pool slot.

The gateway owns admission, quotas and the ledger; a
policy only orders queued request keys. It sees plain values (tenant, root, key, clock time,
cost units), never records, so it stays standard-library only and testable on its own.

Call protocol, all under the gateway lock:
  push(tenant, root, key, now)       a request became QUEUED (submit or restart)
  pick(feasible, now) -> key | None  remove and return the next key the gateway may dispatch;
                                     keys whose request is no longer queued are returned too
                                     (feasible() is True for them) and simply skipped
  on_dispatch(key, tenant, root, now, units)  the picked key really took a slot; units is
                                     the gateway's expected charge for it
  on_finish(key, now, cost_units)    the slot was released with a settled reply

The feedback switch on the client never reaches this module: both modes use the same policy.

The default is `hybrid`; `rr` (the pre-WP5 order), `drr` and `maxmin` are kept for
comparison. Measurements, including the plan's cost-unit DRR that was dropped, are in
docs/engineering/TaskLoom_WP5_L4调度策略设计.md.
"""

from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

from taskloom.policies.fair import FairQueue

PolicyName = Literal["rr", "drr", "maxmin", "hybrid"]


class SchedulingPolicy(Protocol):
    name: str

    def push(self, tenant: str, root: str, key: str, now: float) -> None: ...

    def pick(self, feasible: Callable[[str], bool], now: float) -> str | None: ...

    def on_dispatch(self, key: str, tenant: str, root: str, now: float, units: int = 0) -> None: ...

    def on_finish(self, key: str, now: float, cost_units: int) -> None: ...

    def __len__(self) -> int: ...


class RoundRobinPolicy:
    """Tenant round-robin, then root round-robin, FIFO within a root; no idle credit.

    Equal turns per dispatch: a tenant whose requests hold slots longer gets more slot time.
    """

    name = "rr"

    def __init__(self) -> None:
        self._tenants: FairQueue[str] = FairQueue()
        self._roots: dict[str, FairQueue[str]] = {}

    def push(self, tenant: str, root: str, key: str, now: float) -> None:
        if tenant not in self._roots:
            self._roots[tenant] = FairQueue()
            self._tenants.push(tenant, tenant)
        self._roots[tenant].push(root, key)

    def pick(self, feasible: Callable[[str], bool], now: float) -> str | None:
        for _ in range(len(self._roots)):
            tenant = self._tenants.pop()
            if tenant is None:
                return None
            inner = self._roots[tenant]
            key = inner.pop(feasible)
            if len(inner):
                self._tenants.push(tenant, tenant)
            else:
                del self._roots[tenant]
            if key is not None:
                return key
        return None

    def on_dispatch(self, key: str, tenant: str, root: str, now: float, units: int = 0) -> None:
        pass

    def on_finish(self, key: str, now: float, cost_units: int) -> None:
        pass

    def __len__(self) -> int:
        return sum(len(inner) for inner in self._roots.values())


@dataclass
class _Flow:
    """One tenant, or one root inside a tenant."""

    order: int  # first-seen order, the deterministic tie-break
    charged: float = 0.0  # settled slot-seconds plus no-idle-credit lifts
    running: dict[str, tuple[float, float]] = field(default_factory=dict)  # key -> (start, est)
    queued: int = 0

    def usage(self, now: float) -> float:
        # A running request counts at least its estimate, so one pass cannot hand every free
        # slot to the same flow before anything has finished.
        return self.charged + sum(max(est, now - start) for start, est in self.running.values())

    @property
    def active(self) -> bool:
        return self.queued > 0 or bool(self.running)


@dataclass
class _Tenant(_Flow):
    roots: dict[str, _Flow] = field(default_factory=dict)
    fifo: dict[str, deque[str]] = field(default_factory=dict)
    estimate: float | None = None  # EWMA of this tenant's slot-seconds per request


class OccupancyDrrPolicy:
    """Two-level fair sharing of pool occupancy, measured in slot-seconds. Provisional.

    The contended resource is the pool slot, and a decode-heavy request holds one far longer
    than a short one, so flows are charged for the time they hold slots instead of per
    dispatch. Each flow's usage is its settled slot-seconds plus, for each request it is
    running, max(estimate, elapsed). The next slot goes to the backlogged tenant with the
    least usage, and inside it to the root with the least usage; FIFO within a root, so a
    root's fan-out of agents competes as one flow and earns no extra share.

    No idle credit (virtual-time style): a flow that becomes active again is lifted to the
    least usage among the currently active flows at its level, so absence is not banked.
    Debt is kept while idle. All tenants weigh the same.

    Despite the name this is least-attained-service with a virtual-time floor rather than a
    quantum-based deficit round-robin; the name matches the `drr` configuration value.
    """

    name = "drr"
    ALPHA = 0.2  # EWMA weight of the newest finished request in a tenant's estimate
    DEFAULT_ESTIMATE = 1.0  # slot-seconds assumed before anything has finished
    MAX_ROOTS = 4096  # remembered root flows; idle ones beyond this are forgotten (LRU)

    def __init__(self) -> None:
        self._tenants: dict[str, _Tenant] = {}
        self._running: dict[str, tuple[str, str]] = {}  # key -> (tenant, root)
        self._estimate: float | None = None  # global EWMA, fallback for new tenants
        self._idle_roots: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._count = 0

    def push(self, tenant: str, root: str, key: str, now: float) -> None:
        state, flow = self._flows(tenant, root)
        if not state.active:
            self._lift(state, list(self._tenants.values()), now)
        if not flow.active:
            self._lift(flow, list(state.roots.values()), now)
        state.fifo.setdefault(root, deque()).append(key)
        flow.queued += 1
        state.queued += 1

    def pick(self, feasible: Callable[[str], bool], now: float) -> str | None:
        backlogged = [t for t in self._tenants.items() if t[1].queued]
        for tenant, state in sorted(backlogged, key=lambda t: self._rank(t[1], now)):
            roots = [r for r in state.roots.items() if r[1].queued]
            for root, flow in sorted(roots, key=lambda r: self._rank(r[1], now)):
                items = state.fifo[root]
                # Feasible work can bypass an infeasible head within this root.
                for index, key in enumerate(items):
                    if feasible(key):
                        del items[index]
                        flow.queued -= 1
                        state.queued -= 1
                        if not items:
                            del state.fifo[root]
                            self._maybe_idle(tenant, root)
                        return key
        return None

    def on_dispatch(self, key: str, tenant: str, root: str, now: float, units: int = 0) -> None:
        if key in self._running:
            return
        state, flow = self._flows(tenant, root)
        estimate = state.estimate or self._estimate or self.DEFAULT_ESTIMATE
        state.running[key] = (now, estimate)
        flow.running[key] = (now, estimate)
        self._running[key] = (tenant, root)

    def on_finish(self, key: str, now: float, cost_units: int) -> None:
        owner = self._running.pop(key, None)
        if owner is None:
            return  # never dispatched through this policy (e.g. recovered after a restart)
        tenant, root = owner
        state = self._tenants[tenant]
        start, _ = state.running.pop(key)
        state.roots[root].running.pop(key, None)
        held = max(0.0, now - start)
        state.charged += held
        state.roots[root].charged += held
        state.estimate = self._ewma(state.estimate, held)
        self._estimate = self._ewma(self._estimate, held)
        self._maybe_idle(tenant, root)

    def __len__(self) -> int:
        return sum(state.queued for state in self._tenants.values())

    def usage(self, tenant: str, now: float) -> float:
        """Slot-seconds charged to a tenant so far (diagnostics and tests)."""
        state = self._tenants.get(tenant)
        return state.usage(now) if state else 0.0

    def _rank(self, flow: _Flow, now: float) -> tuple[float, ...]:
        return (flow.usage(now), flow.order)

    def _ewma(self, previous: float | None, value: float) -> float:
        return value if previous is None else (1 - self.ALPHA) * previous + self.ALPHA * value

    def _flows(self, tenant: str, root: str) -> tuple[_Tenant, _Flow]:
        state = self._tenants.get(tenant)
        if state is None:
            state = self._tenants[tenant] = _Tenant(order=len(self._tenants))
        flow = state.roots.get(root)
        if flow is None:
            flow = state.roots[root] = _Flow(order=self._count)
            self._count += 1
        self._idle_roots.pop((tenant, root), None)
        return state, flow

    def _lift(self, flow: _Flow, peers: list[_Flow], now: float) -> None:
        active = [peer.usage(now) for peer in peers if peer is not flow and peer.active]
        if active:
            flow.charged = max(flow.charged, min(active))

    def _maybe_idle(self, tenant: str, root: str) -> None:
        state = self._tenants[tenant]
        if state.roots[root].active:
            return
        self._idle_roots[(tenant, root)] = None
        while len(self._idle_roots) > self.MAX_ROOTS:
            (old_tenant, old_root), _ = self._idle_roots.popitem(last=False)
            del self._tenants[old_tenant].roots[old_root]


class HybridPolicy(OccupancyDrrPolicy):
    """Max-min on the slots held now, then least attained slot-seconds among equals. Default.

    The first key decides who is shortchanged at this instant, which a non-preemptive pool can
    only correct at dispatch time (MaxMinPolicy). Among parties holding the same number of
    slots, the one that has used less recently goes first (OccupancyDrrPolicy, with its
    no-idle-credit lift), which favours light, interactive users over steady heavy ones.
    """

    name = "hybrid"

    def _rank(self, flow: _Flow, now: float) -> tuple[float, ...]:
        return (len(flow.running), flow.usage(now), flow.order)


class _Party:
    """A tenant, or a root inside a tenant: its queue and the slots it holds now."""

    __slots__ = ("held", "queued", "served")

    def __init__(self, served: int) -> None:
        self.held = 0
        self.queued = 0
        self.served = served  # pick sequence number of its last dispatch, for round-robin ties


class MaxMinPolicy:
    """Max-min fair split of the slots held right now: tenants first, then roots in a tenant.

    A free slot goes to the backlogged tenant holding the fewest slots, and within it to the
    backlogged root holding the fewest; FIFO within a root. Ties go to whoever was served
    longest ago, which is round-robin among equals. Without preemption this is the greedy
    step towards the water-filling split: every dispatch raises the lowest backlogged holder.

    Memoryless: no past usage is banked or charged, so a tenant is not punished now for what
    it ran while others were idle, and the state is just the queue plus the in-flight set,
    which a restart rebuilds from records.
    """

    name = "maxmin"

    def __init__(self) -> None:
        self._tenants: dict[str, _Party] = {}
        self._roots: dict[str, dict[str, _Party]] = {}
        self._fifo: dict[tuple[str, str], deque[str]] = {}
        self._running: dict[str, tuple[str, str]] = {}  # key -> (tenant, root)
        self._sequence = 0

    def push(self, tenant: str, root: str, key: str, now: float) -> None:
        state, flow = self._party(tenant, root)
        self._fifo.setdefault((tenant, root), deque()).append(key)
        state.queued += 1
        flow.queued += 1

    def pick(self, feasible: Callable[[str], bool], now: float) -> str | None:
        tenants = [(t, p) for t, p in self._tenants.items() if p.queued]
        for tenant, state in sorted(tenants, key=lambda t: (t[1].held, t[1].served)):
            roots = [(r, p) for r, p in self._roots[tenant].items() if p.queued]
            for root, flow in sorted(roots, key=lambda r: (r[1].held, r[1].served)):
                items = self._fifo[(tenant, root)]
                for index, key in enumerate(items):
                    if feasible(key):
                        del items[index]
                        if not items:
                            del self._fifo[(tenant, root)]
                        state.queued -= 1
                        flow.queued -= 1
                        self._sequence += 1
                        state.served = flow.served = self._sequence
                        self._forget(tenant, root)
                        return key
        return None

    def on_dispatch(self, key: str, tenant: str, root: str, now: float, units: int = 0) -> None:
        if key in self._running:
            return
        state, flow = self._party(tenant, root)
        state.held += 1
        flow.held += 1
        self._running[key] = (tenant, root)

    def on_finish(self, key: str, now: float, cost_units: int) -> None:
        owner = self._running.pop(key, None)
        if owner is None:
            return
        tenant, root = owner
        self._tenants[tenant].held -= 1
        self._roots[tenant][root].held -= 1
        self._forget(tenant, root)

    def __len__(self) -> int:
        return sum(p.queued for p in self._tenants.values())

    def held(self, tenant: str) -> int:
        """Slots a tenant holds now (diagnostics and tests)."""
        state = self._tenants.get(tenant)
        return state.held if state else 0

    def _party(self, tenant: str, root: str) -> tuple[_Party, _Party]:
        # A party that appears joins the back of its ties, as if it had just been served.
        state = self._tenants.get(tenant)
        if state is None:
            state = self._tenants[tenant] = _Party(self._sequence)
            self._roots[tenant] = {}
        flow = self._roots[tenant].get(root)
        if flow is None:
            flow = self._roots[tenant][root] = _Party(self._sequence)
        return state, flow

    def _forget(self, tenant: str, root: str) -> None:
        # Idle parties carry no state worth keeping, so the maps stay as small as the backlog.
        roots = self._roots[tenant]
        if not roots[root].held and not roots[root].queued:
            del roots[root]
        if not roots:
            del self._roots[tenant], self._tenants[tenant]


def scheduling_policy(name: PolicyName) -> SchedulingPolicy:
    policies: dict[str, Callable[[], SchedulingPolicy]] = {
        "rr": RoundRobinPolicy,
        "drr": OccupancyDrrPolicy,
        "maxmin": MaxMinPolicy,
        "hybrid": HybridPolicy,
    }
    return policies[name]()
