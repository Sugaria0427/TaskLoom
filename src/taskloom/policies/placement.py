"""Upstream account placement for the platform's fixed model pool.

The pool is several upstream accounts, each with its own slots and its own prompt cache. A
chain that moves to an account it has not used recently loses most of its cached prefix, so
placement keeps a chain where its cache is warmest:

- Every account a chain has used is remembered with when it last served the chain. A free
  account the chain used more recently holds more of its cache (retention fades over tens
  of minutes, not at a cliff), so a chain that once moved from A to B and finds B full goes
  back to A rather than to an account it never used. Otherwise the least-loaded account.
- Delay scheduling (Zaharia et al., EuroSys 2010): when the account that last served a chain
  is full, its request may step aside for up to `wait_seconds` while others are dispatched,
  and is placed on another account only after that. 0 (the default) places at once: the
  wait saves cache money but counts as unfairness under instantaneous max-min, so it stays
  off until that trade-off is decided (see the WP5 scheduling design record).
"""

from collections import OrderedDict
from collections.abc import Mapping


class AffinityPlacement:
    MAX_CHAINS = 8192  # remembered chains, least recently used dropped first

    def __init__(self, capacities: Mapping[str, int], wait_seconds: float = 0.0) -> None:
        self.capacities = dict(capacities)
        self.wait_seconds = wait_seconds
        self._used: OrderedDict[str, dict[str, float]] = OrderedDict()  # chain -> account -> at
        self._deferred: OrderedDict[str, float] = OrderedDict()  # chain -> first held back

    def choose(self, chain: str, busy: Mapping[str, int], now: float) -> str | None:
        """Pick an account with a free slot for `chain` and remember it; None if all are full."""
        free = [a for a, slots in self.capacities.items() if busy.get(a, 0) < slots]
        if not free:
            return None
        used = self._used.pop(chain, {})
        warm = [a for a in free if a in used]
        if warm:
            account = max(warm, key=lambda a: used[a])
        else:
            # Least loaded by share of its slots in use; configuration order breaks ties.
            account = min(free, key=lambda a: busy.get(a, 0) / self.capacities[a])
        used[account] = now
        self._used[chain] = used
        while len(self._used) > self.MAX_CHAINS:
            self._used.popitem(last=False)
        self._deferred.pop(chain, None)
        return account

    def ready(self, chain: str, busy: Mapping[str, int], now: float) -> bool:
        """Whether a request of `chain` should be placed now rather than wait for its account.

        True when waiting is off, the chain has no account yet, its last account has a free
        slot, or it has already waited `wait_seconds` for it.
        """
        if self.wait_seconds <= 0:
            return True
        used = self._used.get(chain)
        if not used:
            return True
        last = max(used, key=lambda a: used[a])
        if last not in self.capacities or busy.get(last, 0) < self.capacities[last]:
            return True
        first = self._deferred.setdefault(chain, now)
        while len(self._deferred) > self.MAX_CHAINS:
            self._deferred.popitem(last=False)
        return now - first >= self.wait_seconds

    def has_free(self, busy: Mapping[str, int]) -> bool:
        return any(busy.get(a, 0) < slots for a, slots in self.capacities.items())
