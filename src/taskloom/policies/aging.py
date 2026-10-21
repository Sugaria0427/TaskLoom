"""期限紧迫与排队老化叠加在成本轮转之上。

:class:`AgingQueue` 包装 :class:`CostDRR`，在正常轮转之前把快到期限的
和等太久的条目提上来，同时保证提升不会把后台饿死。

三条不变量：

* **提升有上限**：连续提升的次数不超过 ``urgency_burst``，达到上限后强制
  走一轮正常轮转。没有这条，持续涌入的紧急请求会把后台永久挤掉。
* **提升要记账**：被提升的请求照价扣掉所属根任务的额度，所以插队不会白
  拿便宜，下一轮它得先把欠的补回来。
* **紧急优先于老化**：两者同时存在时先提紧急的，期限是硬约束。

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
    ) -> None:
        if quantum <= 0:
            raise ValueError("quantum must be positive")
        if urgency_window <= 0 or aging_window <= 0:
            raise ValueError("windows must be positive")
        if urgency_burst < 1:
            raise ValueError("urgency_burst must be at least 1")
        self._drr: CostDRR[_Slot[T]] = CostDRR(quantum=quantum, cost=lambda slot: cost(slot.item))
        self._cost = cost
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
        """把 ``item`` 排到 ``owner`` 名下并记下入队时刻。

        Args:
            owner: 根任务标识。
            item: 要排队的请求。
        """
        self._insert(owner, item, self._now())

    def pop(self, eligible: Callable[[T], bool] = lambda _: True) -> list[T]:
        """取本轮可以发出的全部条目：先提升，再走一轮正常轮转。

        提升最多连续 ``urgency_burst`` 次。达到上限后本轮剩下的额度全部
        交给正常轮转，所以持续涌入的紧急请求只能拿走固定份额，后台始终
        留有机会。正常轮转之后提升计数归零，下一轮可以重新提。

        Args:
            eligible: 谓词；返回 ``False`` 的条目保持排队。

        Returns:
            本轮发出的条目，先提升后轮转，顺序即派发顺序。一条都没有时
            是空列表；队列未必为空，可能只是额度不够，见 :meth:`blocked`。
        """
        served: list[T] = []
        while self._promoted_in_row < self._urgency_burst:
            owner = self._most_urgent_owner(eligible)
            if owner is None:
                owner = self._most_aged_owner(eligible)
            if owner is None:
                break
            served.append(self._take_from(owner))
            self._promoted_in_row += 1
        self._promoted_in_row = 0
        served.extend(self._serve_round(eligible))
        return served

    def requeue(self, owner: str, item: T, enqueued_at: float) -> None:
        """把没能派发出去的条目放回队首，保留它原来的排队时长。

        调度器拿到一批候选但没有空余并发名额时调用；保留入队时刻是必要
        的，否则反复放回会把老化时钟清零，条目永远等不到老化提升。

        Args:
            owner: 原属根任务。
            item: 要放回的请求。
            enqueued_at: 原入队时刻，与 ``now`` 同一时间域。
        """
        self._insert(owner, item, enqueued_at)

    def remove(self, predicate: Callable[[T], bool]) -> list[tuple[str, T]]:
        """移除所有满足 ``predicate`` 的条目并清掉它们的时刻记录。

        Args:
            predicate: 谓词；返回 ``True`` 的条目被移除。

        Returns:
            按轮转顺序的 ``(根任务, 条目)`` 列表。
        """
        dropped = self._drr.remove(lambda slot: predicate(slot.item))
        pairs: list[tuple[str, T]] = []
        for owner, slot in dropped:
            del self._enqueued_at[slot.push_id]
            pairs.append((owner, slot.item))
        return pairs

    def blocked(self) -> bool:
        """队列里还有条目在等额度凑够。"""
        return self._drr.blocked()

    def next_deadline(self) -> float | None:
        """队列里最早的期限；空队列返回 ``None``。

        调度器空闲时用它决定睡多久，好让等待期间到期的请求按时醒过来
        收尾，而不是一直睡到下一次提交。
        """
        earliest: float | None = None
        for owner in self._drr.owners():
            for slot in self._drr.items(owner):
                value = self._deadline(slot.item)
                if earliest is None or value < earliest:
                    earliest = value
        return earliest

    def deficit(self, owner: str) -> int:
        """``owner`` 当前可用额度。"""
        return self._drr.deficit(owner)

    def refund(self, owner: str, amount: int) -> None:
        """把 ``amount`` 额度退还给 ``owner``。

        派发时按 :class:`ModelRequest` 的预估预留记账，实际消费结算后才
        知道。预留高于实付时把差额退回，避免那些请求白白比别人贵；
        预留低于实付时传负数，等于补记欠账。

        Args:
            owner: 根任务标识。
            amount: 退回的额度，可为负数以表示补记。
        """
        self._drr.refund(owner, amount)

    def __len__(self) -> int:
        """所有根任务排队条目的总数。"""
        return len(self._drr)

    def __contains__(self, owner: str) -> bool:
        """``owner`` 当前是否还有排队条目。"""
        return owner in self._drr

    def _insert(self, owner: str, item: T, enqueued_at: float) -> None:
        slot = _Slot(push_id=self._next_push_id, item=item)
        self._next_push_id += 1
        self._drr.push(owner, slot)
        self._enqueued_at[slot.push_id] = enqueued_at

    def _serve_round(self, eligible: Callable[[T], bool]) -> list[T]:
        """走一轮正常轮转并清掉已发条目的时刻记录。"""
        served = []
        for slot in self._drr.pop(lambda candidate: eligible(candidate.item)):
            del self._enqueued_at[slot.push_id]
            served.append(slot.item)
        return served

    def _take_from(self, owner: str) -> T:
        """从 ``owner`` 提升队首，记账并清掉时刻记录。"""
        slot = self._drr.take(owner)
        if slot is None:
            raise LookupError(f"{owner} has no queued item")
        del self._enqueued_at[slot.push_id]
        return slot.item

    def _most_urgent_owner(self, eligible: Callable[[T], bool]) -> str | None:
        """队首最紧急的一方；没有则 ``None``。"""
        return self._best_owner(eligible, self._urgency_of)

    def _most_aged_owner(self, eligible: Callable[[T], bool]) -> str | None:
        """队首老化最久的一方；没有则 ``None``。"""
        return self._best_owner(eligible, self._age_of)

    def _best_owner(
        self, eligible: Callable[[T], bool], score: Callable[[_Slot[T]], float | None]
    ) -> str | None:
        """按 ``score`` 取分最高的一方，分相同则取先出现的。"""
        best_owner: str | None = None
        best_score = float("-inf")
        for owner in self._drr.owners():
            slot = self._drr.peek(owner)
            if slot is None or not eligible(slot.item):
                continue
            current = score(slot)
            if current is None:
                continue
            if current > best_score:
                best_score = current
                best_owner = owner
        return best_owner

    def _urgency_of(self, slot: _Slot[T]) -> float | None:
        """紧迫度：距期限越近分越高；超过 ``urgency_window`` 返回 ``None``。"""
        remaining = self._deadline(slot.item) - self._now()
        if remaining > self._urgency_window:
            return None
        return -remaining

    def _age_of(self, slot: _Slot[T]) -> float | None:
        """老化度：排队越久分越高；未过 ``aging_window`` 返回 ``None``。"""
        age = self._now() - self._enqueued_at.get(slot.push_id, self._now())
        if age <= self._aging_window:
            return None
        return age
