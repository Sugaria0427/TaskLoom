"""按累计成本做赤字轮转的队列。

:class:`CostDRR` 按某一方已经**被收取的成本**而不是按请求条数来排序。
算法是经典赤字轮转：每次轮到某一方时给它的额度加上 ``quantum``，
队首条目的价格不超过当前额度才出队，额度可以跨轮累积。

成本公平与条数轮转的差别在于，一个一万 token 的请求不再和一个
五十 token 的请求等价。模块只用标准库，内部不含时钟，
调用方通过 ``eligible`` 谓词把期限判断和反馈等待接进来。

额度不足时该方本轮不发，等下一轮继续累积，所以单条价格高于
``quantum`` 的请求不会被永久卡住——它需要 ``ceil(价格/量子)`` 轮
才能发出，代价正比于它的成本，这正是成本公平想要的。

示例:
    >>> q: CostDRR[str] = CostDRR(quantum=10, cost=lambda item: len(item))
    >>> q.push("甲", "xxxx")   # 价格 4
    >>> q.push("乙", "y")      # 价格 1
    >>> q.pop(), q.pop(), q.pop()
    ('xxxx', 'y', None)
"""

from collections import deque
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


class CostDRR[T]:
    """按累计成本轮转的优先级队列。

    每一方有一份"额度"，轮到它时增加 ``quantum``；队首条目价格不超过
    额度才出队并扣减额度。额度跨轮累积且不设上限，因此长期空闲的一方
    回来时不会凭空积攒出一份额度。

    Args:
        quantum: 每轮增加的额度，单位与 ``cost`` 返回值一致。
        cost: 返回条目价格的函数，同时用于扣额度和判断队首是否可发。

    不变量:
        * 每次 ``pop`` 至多访问每一轮环上的每一方一次。
        * 额度不足的一方本轮不发，额度保留到下一轮继续累积。
        * 普通出队不产生欠账，额度始终非负；只有老化层主动提升
          才可能把额度扣成负数，那是刻意的欠账。
    """

    def __init__(self, quantum: int, cost: Callable[[T], int]) -> None:
        if quantum <= 0:
            raise ValueError("quantum must be positive")
        self._quantum = quantum
        self._cost = cost
        self._owners: deque[str] = deque()
        self._items: dict[str, deque[T]] = {}
        self._deficit: dict[str, int] = {}

    @property
    def quantum(self) -> int:
        """每轮增加的额度。"""
        return self._quantum

    def push(self, owner: str, item: T) -> None:
        """把 ``item`` 挂到 ``owner`` 名下排队。

        新的一方加入轮转环，额度从零开始，下一轮轮到时才会增长。
        """
        if owner not in self._items:
            self._items[owner] = deque()
            self._owners.append(owner)
            self._deficit[owner] = 0
        self._items[owner].append(item)

    def pop(self, eligible: Callable[[T], bool] = lambda _: True) -> list[T]:
        """走完一轮轮转环，取出本轮所有付得起的条目。

        每一方被访问一次：额度增加 ``quantum``，然后从队首开始连续发出
        满足 ``eligible`` 且价格不超额度的条目，直到额度用尽或者队首
        付不起。一轮里各方拿到的**成本总量**相同，这才是成本公平；
        如果一轮只发一条，条目少的一方会把额度白白浪费掉，长期份额
        就会偏向请求单价低的一方。

        Args:
            eligible: 谓词；返回 ``False`` 的条目保持排队不发送。

        Returns:
            本轮发出的条目，按轮转顺序排列。一条都没发出时是空列表，
            此时队列未必为空，可能只是额度还不够，见 :meth:`blocked`。
        """
        served: list[T] = []
        for _ in range(len(self._owners)):
            owner = self._owners.popleft()
            items = self._items.get(owner)
            if items is None:
                continue
            self._deficit[owner] += self._quantum
            served.extend(self._serve(owner, items, eligible))
            self._requeue_or_retire(owner, items)
        return served

    def blocked(self) -> bool:
        """队列里还有条目在等额度凑够。

        ``pop`` 返回空列表但本方法为 ``True`` 时，说明不是无事可做
        而是额度不够；调用方应当继续轮转而不是睡死等唤醒。
        """
        return bool(self._items)

    def deficit(self, owner: str) -> int:
        """``owner`` 当前可用额度；未知的一方返回零。"""
        return self._deficit.get(owner, 0)

    def charge(self, owner: str, amount: int) -> None:
        """从 ``owner`` 的额度里扣掉 ``amount``，允许扣成负数。

        老化层主动提升条目时调用，让被插队的请求同样计入成本账本。
        负额度是刻意的欠账，下一轮轮转会先补回来。

        Args:
            owner: 要记账的一方。
            amount: 要扣的成本，必须非负。
        """
        if amount < 0:
            raise ValueError("amount must be non-negative")
        self._deficit[owner] = self._deficit.get(owner, 0) - amount

    def peek(self, owner: str) -> T | None:
        """取 ``owner`` 的队首条目但不删除。

        Args:
            owner: 一方的标识。

        Returns:
            队首条目；该方没有排队条目时返回 ``None``。
        """
        items = self._items.get(owner)
        if not items:
            return None
        return items[0]

    def take(self, owner: str) -> T | None:
        """取出 ``owner`` 的队首条目并记账，不改动轮转环的位置。

        给老化层用：它要越过正常轮转提前发某一条，但仍要扣成本。

        Args:
            owner: 一方的标识。

        Returns:
            队首条目；该方没有排队条目时返回 ``None``。
        """
        items = self._items.get(owner)
        if not items:
            return None
        item = items.popleft()
        self.charge(owner, self._cost(item))
        if not items:
            del self._items[owner]
            del self._deficit[owner]
            if owner in self._owners:
                self._owners.remove(owner)
        return item

    def remove(self, predicate: Callable[[T], bool]) -> list[tuple[str, T]]:
        """跨所有一方移除匹配 ``predicate`` 的条目。

        Returns:
            按轮转顺序返回的 ``(一方, 条目)`` 列表。移除后清空的
            一方会退出轮转环并丢掉累积的额度。
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
                del self._deficit[owner]
                self._owners.remove(owner)
        return removed

    def _serve(self, owner: str, items: deque[T], eligible: Callable[[T], bool]) -> list[T]:
        """发出队首所有付得起的条目，把本轮额度用尽。

        队首不满足 ``eligible`` 就往后看，但价格超额度时立刻停手，
        不越过它去发后面便宜的条目——那会让贵的请求永远排不上。
        """
        served: list[T] = []
        index = 0
        while index < len(items):
            candidate = items[index]
            if not eligible(candidate):
                index += 1
                continue
            price = self._cost(candidate)
            if price > self._deficit[owner]:
                break
            del items[index]
            self._deficit[owner] -= price
            served.append(candidate)
        return served

    def _requeue_or_retire(self, owner: str, items: deque[T]) -> None:
        """一轮结束后，把还有货的一方放回环尾，清空的退场。"""
        if items:
            self._owners.append(owner)
        else:
            del self._items[owner]
            del self._deficit[owner]

    def __len__(self) -> int:
        """所有一方排队条目的总数。"""
        return sum(len(items) for items in self._items.values())

    def __contains__(self, owner: str) -> bool:
        """``owner`` 当前是否还有排队条目。"""
        return owner in self._items

    def owners(self) -> list[str]:
        """按轮转顺序给出当前排队的一方的快照。"""
        return list(self._owners)

    def items(self, owner: str) -> tuple[T, ...]:
        """``owner`` 当前排队条目的快照，不改变队列。"""
        return tuple(self._items.get(owner, ()))
