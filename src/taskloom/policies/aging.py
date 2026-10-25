"""期限紧迫与排队老化叠加在成本轮转之上。

:class:`AgingQueue` 包装 :class:`CostDRR`，在正常轮转之前把快到期限的
和等太久的条目提上来，同时保证提升不会把后台饿死。

四条不变量：

* **提升有上限**：连续提升的次数不超过 ``urgency_burst``，达到上限后强制
  走一轮正常轮转。没有这条，持续涌入的紧急请求会把后台永久挤掉。计数跨
  调用保留，只有真正轮到正常轮转时才归零，所以"取了一堆又放回去"这种做法
  不会把上限冲掉。
* **提升要记账**：被提升的请求照价扣掉所属根任务的额度，所以插队不会白
  拿便宜，下一轮它得先把欠的补回来。
* **紧急优先于老化**：两者同时存在时先提紧急的，期限是硬约束。
* **取出来的就是能派发的**：``pop`` 至多取走 ``limit`` 条，并且只取
  ``accept`` 认可的条目，所以调度器不必再把候选放回队列，也就不会重复
  记账、重复提升。

时间由注入的 ``now`` 提供，模块内不读时钟，纯标准库。

示例:
    >>> clock = iter([100.0]).__next__
    >>> q: AgingQueue[str] = AgingQueue(
    ...     quantum=10,
    ...     cost=lambda x: 1,
    ...     deadline=lambda x: 200.0,
    ...     now=clock,
    ...     urgency_window=60.0,
    ...     aging_window=20.0,
    ...     urgency_burst=2,
    ... )
    >>> q.push("甲", "甲1")
    >>> q.pop()
    ['甲1']
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from taskloom.policies.cost_drr import CostDRR

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class _Slot[T]:
    """排队条目及其入队序号。"""

    push_id: int
    item: T


class AgingQueue[T]:
    """带期限紧迫与排队老化的成本轮转队列。

    Args:
        quantum: 每轮给每个根任务增加的额度，透传给 :class:`CostDRR`。
        cost: 返回条目价格的函数。
        deadline: 返回条目绝对期限的函数，与 ``now`` 同一时间域；没有
            期限的条目返回足够远的将来。
        now: 返回当前时间的函数。
        urgency_window: 距期限不足这个值算紧迫。
        aging_window: 排队超过这个值算老化。
        urgency_burst: 连续提升次数上限，防止紧急请求长期挤掉后台。

    不变量:
        * 结构状态只存在 :class:`CostDRR` 里，本层只维护入队时刻。
        * 一次 :meth:`pop` 最多提升 ``urgency_burst`` 条，之后必走一轮
          正常轮转。
        * 提升走 :meth:`CostDRR.take`，会扣额度，因此提升计入成本账本。
    """

    def __init__(
        self,
        quantum: int,
        cost: Callable[[T], int],
        deadline: Callable[[T], float],
        now: Callable[[], float],
        urgency_window: float,
        aging_window: float,
        urgency_burst: int = 2,
        priority: Callable[[T], int] = lambda _: 0,
    ) -> None:
        if quantum <= 0:
            raise ValueError("quantum must be positive")
        if urgency_window <= 0 or aging_window <= 0:
            raise ValueError("windows must be positive")
        if urgency_burst < 1:
            raise ValueError("urgency_burst must be at least 1")
        self._drr: CostDRR[_Slot[T]] = CostDRR(quantum=quantum, cost=lambda slot: cost(slot.item))
        self._cost = cost
        self._priority = priority
        self._deadline = deadline
        self._now = now
        self._urgency_window = urgency_window
        self._aging_window = aging_window
        self._urgency_burst = urgency_burst
        self._enqueued_at: dict[int, float] = {}
        self._next_push_id = 0
        self._promoted_in_row = 0

    @property
    def quantum(self) -> int:
        """每轮的额度增量，透传给 :class:`CostDRR`。"""
        return self._drr.quantum

    def push(self, owner: str, item: T) -> None:
        """把条目放进所属根任务的队列，队尾等待。"""
        self._insert(owner, item, self._now())

    def pop(
        self,
        eligible: Callable[[T], bool] = lambda _: True,
        limit: int | None = None,
        accept: Callable[[T], bool] | None = None,
    ) -> list[T]:
        """取出至多 ``limit`` 条现在就能派发的条目。

        Args:
            eligible: 过滤仍然有效的条目，被过滤掉的留在队列里。
            limit: 本次最多取出多少条；``None`` 表示不限。
            accept: 判断某一条此刻是否真的可以派发。返回 False 的条目留在
                队首原位，所属方本轮让位，别的方继续走。

        Returns:
            取出的条目，提升的排在轮转的前面。
        """
        served: list[T] = []
        budget = limit
        while self._promoted_in_row < self._urgency_burst:
            if budget is not None and len(served) >= budget:
                break
            owner = self._most_urgent_owner(eligible, accept)
            if owner is None:
                owner = self._most_aged_owner(eligible, accept)
            if owner is None:
                owner = self._most_priority_owner(eligible, accept)
            if owner is None:
                break
            served.append(self._take_from(owner))
            self._promoted_in_row += 1
        if budget is None or len(served) < budget:
            remaining = None if budget is None else budget - len(served)
            rotated = self._serve_round(eligible, remaining, accept)
            served.extend(rotated)
            # 正常轮转真的轮到过了，连续提升的计数才归零；名额被提升占满时
            # 轮转没跑，计数留着，下一次就得先走轮转。
            self._promoted_in_row = 0
        return served

    def requeue(self, owner: str, item: T, enqueued_at: float) -> None:
        """把条目放回所属根任务的队列，保留它已经等待的时长。"""
        self._insert(owner, item, enqueued_at)

    def remove(self, predicate: Callable[[T], bool]) -> list[tuple[str, T]]:
        """移除并返回所有满足条件的条目。

        Args:
            predicate: 判断条目是否该被移除。

        Returns:
            被移除的条目及其所属根任务。
        """
        dropped = self._drr.remove(lambda slot: predicate(slot.item))
        for _, slot in dropped:
            del self._enqueued_at[slot.push_id]
        return [(owner, slot.item) for owner, slot in dropped]

    def ready(self, eligible: Callable[[T], bool] = lambda _: True) -> bool:
        """现在是否取得出东西。

        提升不看额度，所以只要还有紧迫或老化的队首就算可取；否则再问
        :class:`CostDRR` 有没有付得起的一方。调度器据此决定这一轮要不
        要去问平台反馈，避免额度没攒够时把平台读成每轮一次。
        """
        if self._most_urgent_owner(eligible, None) is not None:
            return True
        if self._most_aged_owner(eligible, None) is not None:
            return True
        if self._most_priority_owner(eligible, None) is not None:
            return True
        return self._drr.ready(lambda candidate: eligible(candidate.item))

    def blocked(self) -> bool:
        """队列里还有没有条目。"""
        return self._drr.blocked()

    def next_deadline(self) -> float | None:
        """队列里最早的绝对期限，没有条目时返回 None。"""
        deadlines = [
            self._deadline(slot.item)
            for owner in self._drr.owners()
            for slot in self._drr.items(owner)
        ]
        return min(deadlines) if deadlines else None

    def deficit(self, owner: str) -> int:
        """该根任务当前的可用额度。"""
        return self._drr.deficit(owner)

    def refund(self, owner: str, amount: int) -> None:
        """把多扣的额度还给该根任务。"""
        self._drr.refund(owner, amount)

    def forget(self, owner: str) -> bool:
        """Release a root's carried balance after its workflow has finished."""
        return self._drr.forget(owner)

    def __len__(self) -> int:
        """队列里的条目总数。"""
        return len(self._drr)

    def __contains__(self, owner: str) -> bool:
        """该根任务是否还有条目在排队。"""
        return owner in self._drr

    def _insert(self, owner: str, item: T, enqueued_at: float) -> None:
        """按给定入队时刻放入队列。"""
        slot = _Slot(push_id=self._next_push_id, item=item)
        self._next_push_id += 1
        self._drr.push(owner, slot)
        self._enqueued_at[slot.push_id] = enqueued_at

    def _serve_round(
        self,
        eligible: Callable[[T], bool],
        limit: int | None,
        accept: Callable[[T], bool] | None,
    ) -> list[T]:
        """走一轮正常成本轮转。"""
        served: list[T] = []
        for slot in self._drr.pop(
            lambda candidate: eligible(candidate.item),
            limit,
            None if accept is None else lambda candidate: accept(candidate.item),
        ):
            del self._enqueued_at[slot.push_id]
            served.append(slot.item)
        return served

    def _take_from(self, owner: str) -> T:
        """把该方队首的条目取出来提升，代价记在该方账上。"""
        slot = self._drr.take(owner)
        if slot is None:
            raise LookupError(f"{owner} has no queued item")
        del self._enqueued_at[slot.push_id]
        return slot.item

    def _most_priority_owner(
        self, eligible: Callable[[T], bool], accept: Callable[[T], bool] | None
    ) -> str | None:
        ranks = [
            self._priority(slot.item)
            for owner in self._drr.owners()
            if (slot := self._drr.peek(owner)) is not None
            and eligible(slot.item)
            and (accept is None or accept(slot.item))
        ]
        if not ranks or max(ranks) == min(ranks):
            return None
        return self._best_owner(eligible, lambda slot: self._priority(slot.item), accept)

    def _most_urgent_owner(
        self, eligible: Callable[[T], bool], accept: Callable[[T], bool] | None
    ) -> str | None:
        """期限最紧的根任务。"""
        return self._best_owner(eligible, self._urgency_of, accept)

    def _most_aged_owner(
        self, eligible: Callable[[T], bool], accept: Callable[[T], bool] | None
    ) -> str | None:
        """等得最久的根任务。"""
        return self._best_owner(eligible, self._age_of, accept)

    def _best_owner(
        self,
        eligible: Callable[[T], bool],
        score: Callable[[_Slot[T]], float | None],
        accept: Callable[[T], bool] | None,
    ) -> str | None:
        """按评分挑出最好的一个根任务。"""
        best_owner: str | None = None
        best_score = float("-inf")
        for owner in self._drr.owners():
            slot = self._drr.peek(owner)
            if slot is None or not eligible(slot.item):
                continue
            if accept is not None and not accept(slot.item):
                continue
            current = score(slot)
            if current is None:
                continue
            if current > best_score:
                best_owner = owner
                best_score = current
        return best_owner

    def _urgency_of(self, slot: _Slot[T]) -> float | None:
        """距期限越近分越高，不紧迫的返回 None。"""
        remaining = self._deadline(slot.item) - self._now()
        if remaining > self._urgency_window:
            return None
        return -remaining

    def _age_of(self, slot: _Slot[T]) -> float | None:
        """等得越久分越高，还没到老化门槛的返回 None。"""
        age = self._now() - self._enqueued_at.get(slot.push_id, self._now())
        if age <= self._aging_window:
            return None
        return age
