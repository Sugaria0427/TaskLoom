"""Deadline-aware urgency layer over :class:`CostDRR`.

This module provides :class:`AgingQueue`, which wraps a
:class:`CostDRR` and surfaces "starving" or "near-deadline" items
ahead of normal DRR rotation. The layer enforces three properties
from the WP3 design:

* Background tasks are not starved: an item queued longer than
  ``aging_window`` is promoted before DRR rotates to the next owner.
* Near-deadline items get a bounded urgency boost: an item whose
  remaining time-to-deadline falls below ``urgency_window`` is
  surfaced before DRR's normal selection.
* DRR fairness still applies among non-urgent, non-aged items:
  only the urgent/aged subset is reordered, never the whole queue.

The queue is pure-stdlib; time is supplied by an injected ``now``
callable so the layer remains unit-testable without I/O.

Example:
    >>> clock = iter([100.0]).__next__
    >>> q: AgingQueue[str] = AgingQueue(
    ...     quantum=10, cost=lambda x: 1,
    ...     deadline=lambda x: 200.0, now=clock,
    ...     urgency_window=60.0, aging_window=20.0,
    ... )
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from taskloom.policies.cost_drr import CostDRR

T = TypeVar("T")


@dataclass
class _Slot[T]:
    """Wraps a queued item with a unique push identifier."""

    push_id: int
    item: T


class AgingQueue[T]:
    """Cost DRR with deadline urgency and queue-age aging.

    Args:
        quantum: Per-turn credit increment passed to the underlying
            :class:`CostDRR`.
        cost: Callable returning the cost of a queued item.
        deadline: Callable returning the absolute deadline of a
            queued item, in the same time domain as ``now``. Items
            without a deadline should return a far-future value.
        now: Callable returning the current time.
        urgency_window: Items whose deadline minus ``now()`` falls
            below this value are surfaced ahead of normal DRR.
        aging_window: Items queued longer than this value are
            surfaced ahead of normal DRR when their owner is not
            next in line.

    Invariants:
        * The underlying :class:`CostDRR` is the only source of
          structural state; this layer only adds ordering
          overrides via an internal ``_Slot`` wrapper.
        * Promotion is bounded: at most one urgent or aged item is
          returned per ``pop`` call before falling through to DRR.
    """

    def __init__(
        self,
        quantum: int,
        cost: Callable[[T], int],
        deadline: Callable[[T], float],
        now: Callable[[], float],
        urgency_window: float,
        aging_window: float,
    ) -> None:
        if quantum <= 0:
            raise ValueError("Quantum must be positive")
        if urgency_window <= 0 or aging_window <= 0:
            raise ValueError("Windows must be positive")
        self._drq: CostDRR[_Slot[T]] = CostDRR(
            quantum=quantum, cost=lambda slot: cost(slot.item)
        )
        self._item_cost = cost
        self._deadline = deadline
        self._now = now
        self._urgency_window = urgency_window
        self._aging_window = aging_window
        self._enqueued_at: dict[int, float] = {}
        self._next_push_id = 0

    @property
    def quantum(self) -> int:
        """Per-turn credit increment forwarded from :class:`CostDRR`."""
        return self._drq.quantum

    def push(self, owner: str, item: T) -> None:
        """Enqueue ``item`` and stamp its enqueue time.

        Args:
            owner: Owner identifier passed to the underlying DRR.
            item: Item to enqueue.
        """
        slot = _Slot(push_id=self._next_push_id, item=item)
        self._next_push_id += 1
        self._drq.push(owner, slot)
        self._enqueued_at[slot.push_id] = self._now()

    def pop(self, eligible: Callable[[T], bool] = lambda _: True) -> T | None:
        """Pop the next item with urgency/aging overrides on top of DRR.

        Args:
            eligible: Predicate; an item is only served if it
                returns ``True``.

        Returns:
            The most urgent item if any is within ``urgency_window``
            of its deadline; otherwise the most-aged eligible item
            past ``aging_window``; otherwise whatever
            :class:`CostDRR` returns. Returns ``None`` if nothing is
            eligible.
        """
        urgent_owner = self._most_urgent_owner(eligible)
        if urgent_owner is not None:
            return self._take_from(urgent_owner)
        aged_owner = self._most_aged_owner(eligible)
        if aged_owner is not None:
            return self._take_from(aged_owner)
        return self._pop_drr(eligible)

    def remove(self, predicate: Callable[[T], bool]) -> list[tuple[str, T]]:
        """Remove every item matching ``predicate`` and clean timestamps.

        Returns:
            ``(owner, item)`` pairs in rotation order, with their
            enqueue-time records evicted.
        """
        removed = self._drq.remove(lambda slot: predicate(slot.item))
        cleaned: list[tuple[str, T]] = []
        for owner, slot in removed:
            self._enqueued_at.pop(slot.push_id, None)
            cleaned.append((owner, slot.item))
        return cleaned

    def remaining(self, owner: str) -> int:
        """Carried-over DRR credit for ``owner``."""
        return self._drq.remaining(owner)

    def __len__(self) -> int:
        """Total queued items across all owners."""
        return len(self._drq)

    def __contains__(self, owner: str) -> bool:
        """``True`` if ``owner`` currently has queued items."""
        return owner in self._drq

    def _most_urgent_owner(self, eligible: Callable[[T], bool]) -> str | None:
        """Owner whose head has the smallest remaining time to deadline."""
        now = self._now()
        best_owner: str | None = None
        best_remaining = float("inf")
        for owner in self._drq.owners():
            head = self._drq.peek(owner)
            if head is None or not eligible(head.item):
                continue
            remaining = self._deadline(head.item) - now
            if remaining > self._urgency_window:
                continue
            if remaining < best_remaining:
                best_remaining = remaining
                best_owner = owner
        return best_owner

    def _most_aged_owner(self, eligible: Callable[[T], bool]) -> str | None:
        """Owner whose head has spent the longest in queue past ``aging_window``."""
        now = self._now()
        best_owner: str | None = None
        best_age = 0.0
        for owner in self._drq.owners():
            head = self._drq.peek(owner)
            if head is None or not eligible(head.item):
                continue
            age = now - self._enqueued_at.get(head.push_id, now)
            if age <= self._aging_window:
                continue
            if age > best_age:
                best_age = age
                best_owner = owner
        return best_owner

    def _take_from(self, owner: str) -> T:
        """Take the head item from ``owner`` and clean up its record."""
        slot = self._drq.take(owner)
        assert slot is not None  # caller verified head exists
        self._enqueued_at.pop(slot.push_id, None)
        return slot.item

    def _pop_drr(self, eligible: Callable[[T], bool]) -> T | None:
        """Fall through to :class:`CostDRR` and clean up the dequeued record."""
        slot = self._drq.pop(lambda s: eligible(s.item))
        if slot is None:
            return None
        self._enqueued_at.pop(slot.push_id, None)
        return slot.item