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
  and is placed on another account only after that. It waits only while that account is
  expected to free a slot within the time left: an account held by long decodes (minutes)
  is left at once, since waiting there is starving, not saving. 0 places at once.
- Anticipation (Iyer and Druschel, SOSP 2001): when a chain's call finishes, its slot is kept
  for that chain for up to `anticipate_seconds`, since an agent usually sends its next call
  after a few seconds of thinking. Others see the slot as taken meanwhile. 0 keeps nothing.
"""

from collections import OrderedDict
from collections.abc import Mapping


class AffinityPlacement:
    MAX_CHAINS = 8192  # remembered chains, least recently used dropped first

    def __init__(
        self,
        capacities: Mapping[str, int],
        wait_seconds: float = 0.0,
        anticipate_seconds: float = 0.0,
    ) -> None:
        self.capacities = dict(capacities)
        self.wait_seconds = wait_seconds
        self.anticipate_seconds = anticipate_seconds
        self._used: OrderedDict[str, dict[str, float]] = OrderedDict()  # chain -> account -> at
        self._deferred: OrderedDict[str, float] = OrderedDict()  # chain -> first held back
        self._kept: dict[str, tuple[str, float]] = {}  # chain -> (account, kept until)

    def choose(self, chain: str, busy: Mapping[str, int], now: float) -> str | None:
        """Pick an account with a free slot for `chain` and remember it; None if all are full."""
        free = self._free(chain, busy, now)
        if not free:
            return None
        self._kept.pop(chain, None)
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

    def ready(
        self,
        chain: str,
        busy: Mapping[str, int],
        now: float,
        free_in: Mapping[str, float] | None = None,
    ) -> bool:
        """Whether a request of `chain` should be placed now rather than wait for its account.

        True when waiting is off, the chain has no account yet, its last account has a free
        slot, it has already waited `wait_seconds` for it, or `free_in` (expected seconds
        until each account frees a slot) says the account will not free one in time.
        """
        if self.anticipate_seconds > 0 and not self._free(chain, busy, now):
            return False
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
        left = self.wait_seconds - (now - first)
        return left <= 0 or (free_in is not None and free_in.get(last, 0.0) > left)

    def released(self, chain: str, account: str | None, now: float) -> None:
        """A call of `chain` on `account` finished: keep its slot if anticipation is on."""
        if self.anticipate_seconds > 0 and account in self.capacities:
            self._kept[chain] = (account, now + self.anticipate_seconds)

    def _free(self, chain: str, busy: Mapping[str, int], now: float) -> list[str]:
        """Accounts with a slot free for `chain`, counting slots kept for other chains."""
        left = {a: slots - busy.get(a, 0) for a, slots in self.capacities.items()}
        for other, (account, until) in list(self._kept.items()):
            if until <= now:
                del self._kept[other]
            elif other != chain:
                left[account] -= 1
        return [a for a, slots in left.items() if slots > 0]

    def has_free(self, busy: Mapping[str, int]) -> bool:
        return any(busy.get(a, 0) < slots for a, slots in self.capacities.items())


class HoldEstimator:
    """Expected slot-seconds of a chain's calls: a moving average per chain, falling back to
    the tenant's and then to all calls'."""

    ALPHA = 0.3  # weight of the newest call
    MAX_CHAINS = 8192

    def __init__(self) -> None:
        self._chains: OrderedDict[str, float] = OrderedDict()
        self._tenants: OrderedDict[str, float] = OrderedDict()
        self._all: float | None = None

    def learn(self, tenant: str, chain: str, seconds: float) -> None:
        for table, key in ((self._chains, chain), (self._tenants, tenant)):
            previous = table.pop(key, None)
            table[key] = self._mix(previous, seconds)
            while len(table) > self.MAX_CHAINS:
                table.popitem(last=False)
        self._all = self._mix(self._all, seconds)

    def expect(self, tenant: str, chain: str) -> float | None:
        return self._chains.get(chain, self._tenants.get(tenant, self._all))

    def _mix(self, previous: float | None, value: float) -> float:
        return value if previous is None else (1 - self.ALPHA) * previous + self.ALPHA * value
