"""Two numbers per model request: a worst case that admits, an estimate that is reported.

Costs are in units of one uncached input token. `worst_units` bills every prompt token as a
cache miss and the full max_tokens of output, so budgets that admit on it cannot be overrun.
`ReservationEstimator` predicts the charge from the chain's recent history; ledgers hold it as
the reservation, so feedback and precision reflect what requests really cost.
"""

import math
from collections import OrderedDict
from dataclasses import dataclass


@dataclass(frozen=True)
class Prices:
    """Price of one token relative to one uncached input token."""

    output: float = 1.0
    cache_read: float = 1.0
    cache_write: float = 1.0


def worst_units(input_tokens: int, max_tokens: int, prices: Prices) -> int:
    # Any prompt token may be billed uncached, as a cache write or as a cache read.
    prompt = max(1.0, prices.cache_write, prices.cache_read)
    return math.ceil(input_tokens * prompt + max_tokens * prices.output)


@dataclass(frozen=True)
class Estimate:
    units: int  # expected charge
    tokens: int  # expected input + output tokens


def _remember[K, V](table: OrderedDict[K, V], key: K, value: V, limit: int) -> None:
    """Insert as most recent and evict the least recent beyond `limit`."""
    table[key] = value
    table.move_to_end(key)
    while len(table) > limit:
        table.popitem(last=False)


@dataclass
class _Running:
    """Exponentially weighted mean and mean absolute deviation of recent values."""

    mean: float
    deviation: float

    def add(self, value: float, alpha: float) -> None:
        self.deviation += alpha * (abs(value - self.mean) - self.deviation)
        self.mean += alpha * (value - self.mean)


class ReservationEstimator:
    """Expected charge = uncached input + cached input at the read price + expected output.

    Cached share: a chain (tenant, agent_id) active (dispatched or finished) within WARM_SECONDS
    is warm, else nothing is expected cached; prompt caches fade over about an hour of idling. A warm request expects the cached share that recent warm
    requests of the chain, else the account, else the platform realized (backed out of their
    charges, so upstream moves and cache decay are included), one mean deviation lower; before
    any is known, the share of its prompt the chain sent last time. Expected output: mean plus
    one mean deviation of recent outputs of the chain, else the account, else the platform,
    else max_tokens. Leaning one deviation each way trades a little precision for fewer
    charges above the estimate. State is in memory and bounded; after a restart estimates
    start conservative and relearn.
    """

    WARM_SECONDS = 3600.0
    ALPHA = 0.3
    MAX_ENTRIES = 4096

    def __init__(self, prices: Prices) -> None:
        self.prices = prices
        self._chains: OrderedDict[tuple[str, str], tuple[float, int]] = OrderedDict()
        self._outputs: OrderedDict[tuple[str, ...], _Running] = OrderedDict()
        self._hits: OrderedDict[tuple[str, ...], _Running] = OrderedDict()
        # Whether each dispatched request expected a warm cache, until it settles.
        self._warm: OrderedDict[str, bool] = OrderedDict()

    def _warm_chain(self, tenant: str, agent: str, now: float) -> bool:
        previous = self._chains.get((tenant, agent))
        return previous is not None and now - previous[0] <= self.WARM_SECONDS

    def cached_share(self, tenant: str, agent: str, input_tokens: int, now: float) -> float:
        previous = self._chains.get((tenant, agent))
        if previous is None or not self._warm_chain(tenant, agent, now) or input_tokens <= 0:
            return 0.0
        for key in (("chain", tenant, agent), ("account", tenant), ("global",)):
            hits = self._hits.get(key)
            if hits is not None:
                return max(0.0, hits.mean - hits.deviation)
        return min(1.0, previous[1] / input_tokens)

    def expected_output(self, tenant: str, agent: str | None = None) -> float | None:
        keys = [("account", tenant), ("global",)]
        if agent is not None:
            keys.insert(0, ("chain", tenant, agent))
        for key in keys:
            stats = self._outputs.get(key)
            if stats is not None:
                return stats.mean + stats.deviation
        return None

    def estimate(
        self, tenant: str, agent: str, input_tokens: int, max_tokens: int, now: float
    ) -> Estimate:
        share = self.cached_share(tenant, agent, input_tokens, now)
        expected = self.expected_output(tenant, agent)
        output = max_tokens if expected is None else min(max_tokens, math.ceil(expected))
        prices = self.prices
        # The epsilon keeps float noise in the shares from rounding a whole unit up.
        units = math.ceil(
            input_tokens * (1 - share + share * prices.cache_read) + output * prices.output - 1e-9
        )
        return Estimate(
            units=max(1, min(units, worst_units(input_tokens, max_tokens, prices))),
            tokens=input_tokens + output,
        )

    def dispatched(self, key: str, tenant: str, agent: str, input_tokens: int, now: float) -> None:
        # Learning follows every warm request, even one expected to find nothing cached.
        warm = self._warm_chain(tenant, agent, now)
        _remember(self._warm, key, warm, self.MAX_ENTRIES)
        _remember(self._chains, (tenant, agent), (now, input_tokens), self.MAX_ENTRIES)

    def settled(
        self,
        key: str,
        tenant: str,
        agent: str,
        input_tokens: int,
        output_tokens: int,
        cost_units: int,
        now: float,
    ) -> None:
        previous = self._chains.get((tenant, agent))
        if previous is not None:
            # The cache idles from the end of the request, not from its dispatch.
            _remember(self._chains, (tenant, agent), (now, previous[1]), self.MAX_ENTRIES)
        for scope in (("chain", tenant, agent), ("account", tenant), ("global",)):
            self._learn(self._outputs, scope, output_tokens, spread=0.5)
        warm = self._warm.pop(key, False)
        prices = self.prices
        if warm and input_tokens > 0 and prices.cache_read < 1:
            # Back out the cached share the charge implies; a miss billed as a write reads 0.
            realized = (input_tokens + output_tokens * prices.output - cost_units) / (
                input_tokens * (1 - prices.cache_read)
            )
            for scope in (("chain", tenant, agent), ("account", tenant), ("global",)):
                self._learn(self._hits, scope, min(1.0, max(0.0, realized)), spread=0.0)

    def _learn(
        self,
        table: OrderedDict[tuple[str, ...], _Running],
        key: tuple[str, ...],
        value: float,
        spread: float,
    ) -> None:
        stats = table.get(key)
        if stats is None:
            # One observation leaves the deviation unknown; start it at a share of the value.
            stats = _Running(value, value * spread)
        else:
            stats.add(value, self.ALPHA)
        _remember(table, key, stats, self.MAX_ENTRIES)
