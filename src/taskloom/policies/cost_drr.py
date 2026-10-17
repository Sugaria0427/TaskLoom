"""Cost-based deficit round-robin queue.

This module provides :class:`CostDRR`, a generic priority queue that
schedules items by the cumulative cost an owner has been charged
rather than by item count. The algorithm follows the classical
Deficit Round Robin (DRR) shape: each owner is topped up by
``quantum`` units of credit at the start of its visit; one item is
dequeued if the head fits within the current credit, and unspent
credit carries over (capped at ``quantum``) so that an owner with
small items does not get starved by another owner whose first packet
is larger than ``quantum``.

Cost DRR replaces the count-only fairness of :class:`FairQueue` for
the L3 model request path: a single 10k-token request is not treated
as equal to a 50-token request. The queue is pure-stdlib, contains
no clock, and accepts an eligibility predicate from the caller so
it can interact with deadline checks and feedback waits.

Example:
    >>> q: CostDRR[str] = CostDRR(quantum=10, cost=lambda item: len(item))
    >>> q.push("alice", "xxxx")  # cost 4
    >>> q.push("bob", "y")      # cost 1
    >>> q.pop(), q.pop(), q.pop()
    ('xxxx', 'y', None)
"""

from collections import deque
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


class CostDRR[T]:
    """Round-robin among owners by accumulated cost credit.

    Each owner has a "remaining" credit that grows by ``quantum`` at the
    start of its turn. Items are dequeued from the owner while the
    running total cost stays within that credit; the leftover credit
    carries to the next turn, capped at ``quantum`` so an idle owner
    cannot stockpile arbitrarily large credit when it returns.

    Args:
        quantum: Per-turn credit increment. Measured in the same units
            as the values returned by ``cost``.
        cost: Callable returning the cost of a queued item. Used both
            for charging the owner and for checking whether the current
            item fits within the active credit.

    Invariants:
        * Each owner is visited at most once per ``pop`` call; an owner
          whose head cannot be served keeps its topped-up credit and
          returns to the back of the rotation.
        * Items larger than ``quantum`` are never served while they
          sit at the head of an owner's queue.
        * An owner's remaining credit never exceeds ``quantum``.
    """

    def __init__(self, quantum: int, cost: Callable[[T], int]) -> None:
        if quantum <= 0:
            raise ValueError("Quantum must be positive")
        self._quantum = quantum
        self._cost = cost
        self._owners: deque[str] = deque()
        self._items: dict[str, deque[T]] = {}
        self._remaining: dict[str, int] = {}

    @property
    def quantum(self) -> int:
        """Per-turn credit increment."""
        return self._quantum

    def push(self, owner: str, item: T) -> None:
        """Enqueue ``item`` on behalf of ``owner``.

        The owner is added to the rotation if new; credit starts at
        zero and is topped up at the start of the owner's next turn.
        """
        if owner not in self._items:
            self._items[owner] = deque()
            self._owners.append(owner)
            self._remaining[owner] = 0
        self._items[owner].append(item)

    def pop(self, eligible: Callable[[T], bool] = lambda _: True) -> T | None:
        """Pop the next item under DRR fairness.

        Walks the full rotation at most once per call: each visited
        owner is topped up by ``quantum`` (credit capped at
        ``quantum``), and the first eligible head that fits within
        that credit is sent. Owners whose heads do not fit, or whose
        queues are empty after the visit, are moved to the back of
        the rotation or removed respectively.

        Args:
            eligible: Predicate; an item is only served if it returns
                ``True``. Items failing the predicate remain queued.

        Returns:
            The served item, or ``None`` if no owner has an eligible
            head that fits in its current credit.
        """
        for _ in range(len(self._owners)):
            owner = self._owners.popleft()
            if owner not in self._items:
                continue
            items = self._items[owner]
            self._remaining[owner] = min(
                self._quantum, self._remaining[owner] + self._quantum
            )
            sent = self._try_send(owner, items, eligible)
            if sent is not None:
                if items:
                    self._owners.append(owner)
                else:
                    del self._items[owner]
                    del self._remaining[owner]
                return sent
            if items:
                self._owners.append(owner)
        return None

    def _try_send(
        self, owner: str, items: deque[T], eligible: Callable[[T], bool]
    ) -> T | None:
        """Send the first eligible head, draining credit for oversize items.

        A head whose price exceeds current credit is still served: DRR
        fairness governs amortized cost over the rotation, not single
        item size. Serving an oversize head drains the owner's credit
        to zero so the next request in the same rotation must wait
        for a fresh top-up.
        """
        index = 0
        while index < len(items):
            candidate = items[index]
            if not eligible(candidate):
                index += 1
                continue
            price = self._cost(candidate)
            del items[index]
            self._remaining[owner] = max(0, self._remaining[owner] - price)
            return candidate
        return None

    def peek(self, owner: str) -> T | None:
        """Return the head item for ``owner`` without removing it.

        Args:
            owner: Owner identifier.

        Returns:
            Head item, or ``None`` if the owner has no queued items.
        """
        items = self._items.get(owner)
        if not items:
            return None
        return items[0]

    def take(self, owner: str) -> T | None:
        """Pop the head item for ``owner`` without touching the rotation.

        Used by layers (aging, urgency) that want to promote a specific
        owner's head ahead of the normal DRR rotation.

        Args:
            owner: Owner identifier.

        Returns:
            The head item, or ``None`` if the owner has no queued items.
            The owner's rotation position is unchanged on ``None``;
            otherwise the item is removed and remaining credit stays.
        """
        items = self._items.get(owner)
        if not items:
            return None
        item = items.popleft()
        if not items:
            del self._items[owner]
            del self._remaining[owner]
            if owner in self._owners:
                self._owners.remove(owner)
        return item

    def remove(self, predicate: Callable[[T], bool]) -> list[tuple[str, T]]:
        """Remove every item matching ``predicate`` across all owners.

        Returns:
            ``(owner, item)`` pairs in rotation order. Owners whose
            queues become empty are dropped from the rotation and lose
            any carried credit.
        """
        removed: list[tuple[str, T]] = []
        for owner in list(self._owners):
            keep: deque[T] = deque()
            for item in self._items[owner]:
                if predicate(item):
                    removed.append((owner, item))
                else:
                    keep.append(item)
            if keep:
                self._items[owner] = keep
            else:
                del self._items[owner]
                del self._remaining[owner]
                self._owners.remove(owner)
        return removed

    def remaining(self, owner: str) -> int:
        """Carried-over credit for ``owner``; zero if owner is unknown."""
        return self._remaining.get(owner, 0)

    def __len__(self) -> int:
        """Total queued items across all owners."""
        return sum(len(items) for items in self._items.values())

    def __contains__(self, owner: str) -> bool:
        """``True`` if ``owner`` currently has queued items."""
        return owner in self._items

    def owners(self) -> list[str]:
        """Snapshot of currently-queued owner identifiers in rotation order."""
        return list(self._owners)