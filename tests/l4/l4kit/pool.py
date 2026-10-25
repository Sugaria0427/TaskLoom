"""Scripted stand-in for the fixed model pool behind the platform's ModelProvider port.

The pool is one or more upstream accounts, each with its own slots and its own prompt cache.
With a `retention` model, a chain's cached prompt survives only on the accounts that served it,
fading with idle time; a call placed elsewhere pays for the part that misses. The gateway
names the account when its PlatformConfig lists pool accounts (ids `account_ids`: "a0", "a1",
...). Without them it passes none, and `placement` stands in for whoever chooses: "rotate" is
a round-robin balancer that ignores caches, "sticky" prefers the account that last served the
chain.
"""

import asyncio
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Literal

from taskloom.contracts.models import ModelReply, ModelRequest, Usage
from taskloom.gateway.ports import ProviderRejected

# reject: refused before execution, no receipt (pool busy, 4xx).
# lost: executed and billed, reply lost in transit; a receipt is queryable.
# lost_forever: executed and billed, no receipt (a backend without request lookup).
Fault = Literal["none", "reject", "lost", "lost_forever"]


class PoolRejected(ProviderRejected):
    pass


class ReplyLost(ConnectionError):
    pass


@dataclass(frozen=True)
class Behavior:
    output_tokens: int | None = None
    service_seconds: float | None = None
    cost_units: int | None = None
    fault: Fault = "none"
    cached_tokens: int = 0
    cache_discount: float = 0.0
    cold_seconds: float = 0.0
    # Start-to-start gap the trace had before this call, when its cache hits were recorded.
    trace_gap: float | None = None


@dataclass(frozen=True)
class PoolCall:
    pool_request_id: str
    attempt_id: str
    root_task_id: str
    started: float
    finished: float
    cost_units: int
    fault: Fault
    account: int = 0
    chain: str = ""
    first: bool = True  # the chain's first call anywhere: its cache state is unknown
    moved: bool = False  # placed on another account than the chain's previous call
    cached_tokens: int = 0  # what the trace read from cache
    retained: float = 1.0  # share of it still cached on this account
    extra_units: int = 0  # cost of the part that missed


Placement = Literal["rotate", "sticky"]


@dataclass
class ScriptedPool:
    capacity: int = 2
    accounts: tuple[int, ...] = ()  # slots per upstream account; empty is one of `capacity`
    placement: Placement = "rotate"
    # Share of a warm prefix still cached after an idle time (None: never served); None
    # ignores caching and charges every call as planned.
    retention: Callable[[float | None], float] | None = None
    base_seconds: float = 0.2
    seconds_per_output_token: float = 0.01
    default_output_tokens: int = 64
    behaviors: dict[str, Behavior] = field(default_factory=dict)
    calls: Counter[str] = field(default_factory=Counter)  # executions per pool request id
    refusals: Counter[str] = field(default_factory=Counter)
    receipts: dict[str, ModelReply] = field(default_factory=dict)
    log: list[PoolCall] = field(default_factory=list)
    active: int = 0
    running: dict[str, float] = field(default_factory=dict)  # pool id -> loop start time
    peak: int = 0
    reconcile_calls: int = 0
    busy: list[int] = field(default_factory=list)
    touched: dict[str, dict[int, float]] = field(default_factory=dict)  # chain -> account -> t
    previous: dict[str, int] = field(default_factory=dict)  # chain -> last account
    turn: int = 0

    def __post_init__(self) -> None:
        if self.accounts:
            self.capacity = sum(self.accounts)
        else:
            self.accounts = (self.capacity,)
        self.busy = [0] * len(self.accounts)

    @property
    def account_ids(self) -> tuple[str, ...]:
        return tuple(f"a{index}" for index in range(len(self.accounts)))

    def place(self, chain: str) -> int | None:
        free = [a for a, slots in enumerate(self.accounts) if self.busy[a] < slots]
        if not free:
            return None
        history = self.touched.get(chain, {})
        if self.placement == "sticky":
            warm = [a for a in free if a in history]
            if warm:
                return max(warm, key=lambda a: history[a])
        pick = min(free, key=lambda a: (a - self.turn) % len(self.accounts))
        self.turn = pick + 1
        return pick

    def plan(self, attempt_id: str, behavior: Behavior) -> None:
        self.behaviors[attempt_id] = behavior

    async def generate(self, request: ModelRequest, account: str | None = None) -> ModelReply:
        loop = asyncio.get_running_loop()
        behavior = self.behaviors.get(request.attempt_id, Behavior())
        chain = request.agent_id
        if behavior.fault == "reject":
            index = None
        elif account is None:
            index = self.place(chain)
        else:
            # The gateway chose; a full account means it overbooked, and the pool refuses.
            index = self.account_ids.index(account)
            if self.busy[index] >= self.accounts[index]:
                index = None
        if index is None:
            # Nothing ran, so a later dispatch of the same request is not a replay.
            self.refusals[request.request_id] += 1
            raise PoolRejected("pool refused the request before execution")
        self.calls[request.request_id] += 1
        started = loop.time()
        history = self.touched.setdefault(chain, {})
        first = not history
        if self.retention is None or first:
            retained = 1.0
        else:
            last = history.get(index)
            retained = self.retention(None if last is None else started - last)
            if behavior.trace_gap is not None:
                # The trace's hits already reflect its own gap on its own account; charge only
                # what placement and queueing lose beyond that.
                retained = min(1.0, retained / self.retention(behavior.trace_gap))
        missed = 1.0 - retained
        extra = round(behavior.cached_tokens * missed * behavior.cache_discount)
        moved = not first and self.previous.get(chain) != index
        history[index] = started
        self.previous[chain] = index
        self.busy[index] += 1
        self.running[request.request_id] = started
        self.active += 1
        self.peak = max(self.peak, self.active)
        output = (
            behavior.output_tokens
            if behavior.output_tokens is not None
            else min(request.max_tokens, self.default_output_tokens)
        )
        service = (
            behavior.service_seconds
            if behavior.service_seconds is not None
            else self.base_seconds + output * self.seconds_per_output_token
        ) + missed * behavior.cold_seconds
        cost = (
            behavior.cost_units
            if behavior.cost_units is not None
            else request.input_tokens + output
        ) + extra
        placed = PoolCall(
            request.request_id, request.attempt_id, request.root_task_id, started, started,
            cost, behavior.fault, index, chain, first, moved, behavior.cached_tokens,
            retained, extra,
        )  # fmt: skip
        try:
            await asyncio.sleep(service)
        except asyncio.CancelledError:
            # The caller hung up mid-generation. The pool stops, bills the prefill and the
            # share of output already produced, and keeps a receipt for reconciliation.
            done = (loop.time() - started) / service if service else 1.0
            produced = round(output * done)
            prefill = request.input_tokens + extra
            partial = round(prefill + (cost - prefill) * done)
            self._settle(request, placed, produced, min(cost, partial), keep=True)
            raise
        finally:
            self.active -= 1
            self.busy[index] -= 1
            self.running.pop(request.request_id, None)
        reply = self._settle(request, placed, output, cost, keep=behavior.fault != "lost_forever")
        if behavior.fault in ("lost", "lost_forever"):
            raise ReplyLost("reply lost after execution")
        return reply

    def _settle(
        self, request: ModelRequest, placed: PoolCall, output: int, cost: int, keep: bool
    ) -> ModelReply:
        reply = ModelReply(
            content="{}",
            usage=Usage(input_tokens=request.input_tokens, output_tokens=output, cost_units=cost),
        )
        finished = asyncio.get_running_loop().time()
        self.log.append(replace(placed, finished=finished, cost_units=cost))
        if keep:
            self.receipts[request.request_id] = reply
        return reply

    async def reconcile(self, request_id: str, account: str | None = None) -> ModelReply | None:
        self.reconcile_calls += 1
        return self.receipts.get(request_id)

    async def close(self) -> None:
        pass
