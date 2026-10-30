from collections import deque
from collections.abc import Callable


class FairQueue[T]:
    """Round-robin among owners, FIFO within each owner; no idle credit."""

    def __init__(self) -> None:
        self._owners: deque[str] = deque()
        self._items: dict[str, deque[T]] = {}

    def push(self, owner: str, item: T) -> None:
        if owner not in self._items:
            self._items[owner] = deque()
            self._owners.append(owner)
        self._items[owner].append(item)

    def pop(self, eligible: Callable[[T], bool] = lambda _: True) -> T | None:
        for _ in range(len(self._owners)):
            owner = self._owners.popleft()
            items = self._items[owner]
            selected: T | None = None
            # Feasible work can bypass an infeasible head within this owner's turn.
            for index, candidate in enumerate(items):
                if eligible(candidate):
                    selected = candidate
                    del items[index]
                    break
            if items:
                self._owners.append(owner)
            else:
                del self._items[owner]
            if selected is not None:
                return selected
        return None

    def remove(self, predicate: Callable[[T], bool]) -> list[T]:
        removed: list[T] = []
        for owner in list(self._owners):
            keep: deque[T] = deque()
            for item in self._items[owner]:
                (removed if predicate(item) else keep).append(item)
            if keep:
                self._items[owner] = keep
            else:
                del self._items[owner]
                self._owners.remove(owner)
        return removed

    def peek(self, eligible: Callable[[T], bool] = lambda _: True) -> T | None:
        """Return the next feasible item without rotating or removing any owner."""
        for owner in self._owners:
            for item in self._items[owner]:
                if eligible(item):
                    return item
        return None

    def __len__(self) -> int:
        return sum(len(items) for items in self._items.values())

    def items(self) -> list[T]:
        """Observe candidates without changing owner turns or FIFO positions."""
        return [item for owner in self._owners for item in self._items[owner]]
