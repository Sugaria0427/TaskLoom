"""按累计成本做赤字轮转的队列。

:class:`CostDRR` 按某一方已经**被收取的成本**而不是按请求条数来排序。
算法是经典赤字轮转：每轮轮到某一方时给它的额度加上 ``quantum``，
队首条目的价格不超过当前额度才出队，额度可以跨轮累积。

一轮（traversal）指轮转环上每一方都做完自己的回合：一方发到付不起为止，
它的回合才算结束，轮转环才继续往下走。额度**按轮发放**而不是按调用发放，
所以调度器一次只派发少量请求时，额度不会被提前透支，各方的成本份额
依然均衡——这正是复审里"重请求派发 20 次、轻请求 180 次"那种失衡的
根因修复点。

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

    每一方有一份"额度"，每轮轮到它时增加 ``quantum``；队首条目价格不超过
    额度才出队并扣减额度。额度跨轮累积且不设上限，因此长期空闲的一方
    回来时不会凭空积攒出一份额度。

    Args:
        quantum: 每轮增加的额度，单位与 ``cost`` 返回值一致。
        cost: 返回条目价格的函数，同时用于扣额度和判断队首是否可发。

    不变量:
        * 一轮里每一方只领一次额度，额度按轮发放而不是按调用发放。
        * 一方的回合在它发到付不起、或队首被 :meth:`pop` 的 ``accept``
          拒绝时结束，轮转环因此始终往下走，不会被一个积压的大户卡死。
        * 额度不足的一方本轮不发，额度保留到下一轮继续累积。
        * 普通出队不产生欠账，额度始终非负；只有老化层主动提升
          才可能把额度扣成负数，那是刻意的欠账。
        * 队列暂空时保留提升债务，结算差额与债务共同结转到后续步骤。
          未使用的正向量子在退场时清除，根任务结束时显式回收结转。
    """

    def __init__(self, quantum: int, cost: Callable[[T], int]) -> None:
        if quantum <= 0:
            raise ValueError("quantum must be positive")
        self._quantum = quantum
        self._cost = cost
        self._owners: deque[str] = deque()
        self._items: dict[str, deque[T]] = {}
        self._deficit: dict[str, int] = {}
        self._carried: dict[str, int] = {}
        self._visited: set[str] = set()
        self._turn_done: set[str] = set()

    @property
    def quantum(self) -> int:
        """每轮增加的额度。"""
        return self._quantum

    def push(self, owner: str, item: T) -> None:
        """把 ``item`` 挂到 ``owner`` 名下排队。

        新的一方加入轮转环，额度从结转值开始，下一轮轮到时才会增长。
        """
        if owner not in self._items:
            self._items[owner] = deque()
            self._owners.append(owner)
            self._deficit[owner] = self._carried.pop(owner, 0)
        self._items[owner].append(item)

    def pop(
        self,
        eligible: Callable[[T], bool] = lambda _: True,
        limit: int | None = None,
        accept: Callable[[T], bool] | None = None,
    ) -> list[T]:
        """走一轮轮转，取出本轮付得起且准发的条目。

        每一方在一轮里只领一次额度，随后从队首开始连续发出满足
        ``eligible``、被 ``accept`` 认可、且价格不超额度的条目，直到额度
        用尽或者队首付不起。一轮里各方拿到的**成本总量**相同，这才是
        成本公平；如果一轮只发一条，条目少的一方会把额度白白浪费掉，
        长期份额就会偏向请求单价低的一方。

        Args:
            eligible: 谓词；返回 ``False`` 的条目保持排队不发送。
            limit: 本次最多发出多少条。``None`` 表示不限。名额用完时
                停在当前一方，下次调用从它继续，轮转进度不丢。
            accept: 谓词；返回 ``False`` 表示这个候选现在不能派发
                （比如它自己要的钱超过账户当前可用额度）。这一方本轮
                让位，条目留在原位，既不扣额度也不改变它的排队次序。

        Returns:
            本轮发出的条目，按轮转顺序排列。一条都没发出时是空列表，
            此时队列未必为空，可能只是额度还不够，见 :meth:`blocked`。
        """
        served: list[T] = []
        budget = limit
        while self._owners:
            if budget is not None and budget <= 0:
                break
            owner = self._owners[0]
            if owner not in self._visited:
                self._visited.add(owner)
                self._deficit[owner] += self._quantum
            items = self._items[owner]
            emitted = self._serve(owner, items, eligible, accept, budget)
            served.extend(emitted)
            if budget is not None:
                budget -= len(emitted)
            if items:
                if budget is not None and budget <= 0:
                    # 名额用尽，这一方的回合还没做完，停在原地下次接着来。
                    break
                # 额度不够或者队首被拒：这一方本轮让位，额度留着下轮再用。
                self._owners.rotate(-1)
                self._turn_done.add(owner)
            else:
                self._retire(owner)
            if all(done in self._turn_done for done in self._owners):
                self._visited.clear()
                self._turn_done.clear()
                break
        return served

    def _retire(self, owner: str) -> None:
        """Leave the queue while retaining promotion debt for later workflow steps."""
        self._items.pop(owner, None)
        debt = min(0, self._deficit.pop(owner, 0))
        if debt:
            self._carried[owner] = self._carried.get(owner, 0) + debt
        if owner in self._owners:
            self._owners.remove(owner)
        self._visited.discard(owner)
        self._turn_done.discard(owner)

    def forget(self, owner: str) -> bool:
        """Release a completed root's carried balance; queued work retains its share."""
        if owner in self._items:
            return False
        self._carried.pop(owner, None)
        return True

    def ready(self, eligible: Callable[[T], bool] = lambda _: True) -> bool:
        """至少有一方现在付得起它的队首条目。

        与 :meth:`pop` 用同一套判据，但只读不改：调度器可以据此判断现在
        值不值得去问平台。不值得的时候先让轮转把额度发出来，别把平台
        反馈读成每轮一次。

        Args:
            eligible: 谓词；返回 ``False`` 的条目不算数。

        Returns:
            有一方付得起返回 ``True``。
        """
        for owner in self._owners:
            for candidate in self._items[owner]:
                if not eligible(candidate):
                    continue
                if self._cost(candidate) <= self._deficit.get(owner, 0):
                    return True
                # 队首付不起时 pop 不会越过它，后面的条目也不用看了。
                break
        return False

    def blocked(self) -> bool:
        """队列里还有条目在等额度凑够。

        ``pop`` 返回空列表但本方法为 ``True`` 时，说明不是无事可做
        而是额度不够；调用方应当继续轮转而不是睡死等唤醒。
        """
        return bool(self._items)

    def deficit(self, owner: str) -> int:
        """``owner`` 当前可用额度；未知的一方返回零。"""
        return self._deficit.get(owner, 0)

    def refund(self, owner: str, amount: int) -> None:
        """把 ``amount`` 额度退还给 ``owner``，``amount`` 可为负数。

        负数等价于 :meth:`charge`，正数把多预扣的部分还给这一方。

        队列里没有的一方走结转账本：该方多半刚把最后一条发出去，
        提升债务仍需偿还，结算差额与剩余债务共同决定下一阶段的额度。
        结转值在它重新 :meth:`push` 时并入初始额度。

        Args:
            owner: 要记账的一方。
            amount: 退回的额度，负数表示补记。
        """
        if owner in self._items:
            self._deficit[owner] += amount
        elif amount:
            self._carried[owner] = self._carried.get(owner, 0) + amount

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
            self._retire(owner)
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
                self._retire(owner)
        return removed

    def _serve(
        self,
        owner: str,
        items: deque[T],
        eligible: Callable[[T], bool],
        accept: Callable[[T], bool] | None,
        budget: int | None,
    ) -> list[T]:
        """发出队首所有付得起且准发的条目，把本轮额度用尽。

        队首不满足 ``eligible`` 就往后看，但价格超额度时立刻停手，
        不越过它去发后面便宜的条目——那会让贵的请求永远排不上。
        ``accept`` 拒绝队首时整方让位，同样不越过它去发后面的条目，
        免得同一根任务的请求次序被打乱。
        """
        served: list[T] = []
        index = 0
        while index < len(items):
            if budget is not None and len(served) >= budget:
                break
            candidate = items[index]
            if not eligible(candidate):
                index += 1
                continue
            if accept is not None and not accept(candidate):
                break
            price = self._cost(candidate)
            if price > self._deficit[owner]:
                break
            del items[index]
            self._deficit[owner] -= price
            served.append(candidate)
        return served

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
