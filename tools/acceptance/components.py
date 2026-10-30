"""Small, bounded demonstrations of actual production policy components."""

import asyncio
from collections import Counter
from typing import Any

from taskloom.client.dispatcher import Dispatcher
from taskloom.domain.model import Resources
from taskloom.policies.access import KeyedMutex
from taskloom.policies.aging import AgingQueue
from taskloom.policies.banker import PhaseBanker
from taskloom.policies.cost_drr import CostDRR


def aging() -> dict[str, Any]:
    clock = [0.0]
    queue: AgingQueue[tuple[str, int, float]] = AgingQueue(
        quantum=10,
        cost=lambda item: item[1],
        deadline=lambda item: item[2],
        now=lambda: clock[0],
        urgency_window=5,
        aging_window=20,
        urgency_burst=2,
    )
    queue.push("background", ("后台任务", 1000, 10000))
    clock[0] = 21.0
    queue.push("new", ("刚到的短任务", 1, 10000))
    selected = queue.pop(limit=1)
    assert selected and selected[0][0] == "后台任务"
    aged_debt = queue.deficit("background")
    urgent: AgingQueue[str] = AgingQueue(
        quantum=10,
        cost=lambda _: 1,
        deadline=lambda item: 25 if item.startswith("紧急") else 10000,
        now=lambda: clock[0],
        urgency_window=5,
        aging_window=20,
        urgency_burst=2,
    )
    urgent.push("normal", "普通任务")
    for index in range(4):
        urgent.push("urgent", "紧急任务" + str(index + 1))
    order = [urgent.pop(limit=1)[0] for _ in range(3)]
    assert order[:2] == ["紧急任务1", "紧急任务2"] and order[2] == "普通任务"
    return {
        "说明": "复用 AgingQueue，以可控时钟推进 21 秒，不占用真实 API。",
        "老化后优先执行": selected[0][0],
        "提升后的成本余额": aged_debt,
        "连续紧急请求中的派发顺序": order,
        "结论": "老任务获得提升；两次紧急提升后，普通成本轮转获得机会。",
    }


def fairness() -> dict[str, Any]:
    queue: CostDRR[tuple[str, int]] = CostDRR(quantum=1000, cost=lambda item: item[1])
    for owner, price in (("长请求", 1000), ("短请求", 100)):
        for _ in range(300):
            queue.push(owner, (owner, price))
    counts: Counter[str] = Counter()
    costs: Counter[str] = Counter()
    for _ in range(1000):
        selected = queue.pop(limit=1)
        if not selected:
            continue
        owner, price = selected[0]
        counts[owner] += 1
        costs[owner] += price
        if counts.total() == 220:
            break
    assert costs["长请求"] == costs["短请求"] and counts["短请求"] == 10 * counts["长请求"]
    return {
        "说明": "两方保持积压，单次成本分别为 1000 与 100。",
        "DRR 派发次数": dict(counts),
        "DRR 服务成本": dict(costs),
        "等次数轮转的成本对照": {"长请求": 110000, "短请求": 11000},
        "结论": "成本公平允许短请求获得更多次机会，两方总服务成本相同。",
    }


def banker() -> dict[str, Any]:
    def instance(safe: bool) -> PhaseBanker:
        policy = PhaseBanker(Resources(slots=3, memory_bytes=10), check_safe=safe)
        for name in ("任务甲", "任务乙"):
            policy.declare(name, Resources(slots=1, memory_bytes=10))
        return policy

    safe, greedy = instance(True), instance(False)
    for policy in (safe, greedy):
        assert policy.request("任务甲", Resources(slots=1, memory_bytes=4))
    refused = not safe.request("任务乙", Resources(slots=1, memory_bytes=4))
    assert refused and greedy.request("任务乙", Resources(slots=1, memory_bytes=4))
    assert greedy.safe() is None
    reason = safe.rejections[-1].reason
    safe.release("任务甲")
    assert safe.request("任务乙", Resources(slots=1, memory_bytes=4))
    return {
        "说明": "内存总量 10，两个任务的最大需求均为 10，先各申请 4。",
        "贪心分配": "两次都批准，剩余 2，双方都还需要 6，无安全序列。",
        "银行家分配": "拒绝第二次申请，保留安全完成路径。",
        "拒绝原因": reason,
        "甲释放后乙可获批准": True,
        "当前安全序列": safe.safe(),
        "层级": "独立银行家组件演示，不修改真实 Agent 资源分配器。",
    }


async def deadlock() -> dict[str, Any]:
    left, right = asyncio.Lock(), asyncio.Lock()
    left_held, right_held = asyncio.Event(), asyncio.Event()

    async def reverse(
        first: asyncio.Lock, second: asyncio.Lock, held: asyncio.Event, other: asyncio.Event
    ) -> None:
        async with first:
            held.set()
            await other.wait()
            async with second:
                pass

    tasks = [
        asyncio.create_task(reverse(left, right, left_held, right_held)),
        asyncio.create_task(reverse(right, left, right_held, left_held)),
    ]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=0.3)
        raise AssertionError("应构成相反锁序的循环等待")
    except TimeoutError:
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert not left.locked() and not right.locked()
    mutex = KeyedMutex()
    finished: list[str] = []

    async def ordered(name: str, keys: list[str]) -> None:
        async with mutex.locks(keys):
            await asyncio.sleep(0.02)
            finished.append(name)

    await asyncio.wait_for(
        asyncio.gather(ordered("甲", ["a", "b"]), ordered("乙", ["b", "a"])), timeout=2
    )
    assert set(finished) == {"甲", "乙"}
    return {
        "循环等待": "甲持有 a 等 b；乙持有 b 等 a，超时后演示协程自动清理。",
        "统一锁序后的完成任务": finished,
        "结论": "有序多锁破坏循环等待条件；演示结束时所有锁已释放。",
        "层级": "独立 asyncio 锁及 KeyedMutex 组件，不锁住真实服务。",
    }


async def resources() -> dict[str, Any]:
    dispatcher = Dispatcher(Resources(slots=2, memory_bytes=384))
    entered, finish = asyncio.Event(), asyncio.Event()
    order: list[str] = []

    async def hold() -> str:
        order.append("占用 256 的长工具")
        entered.set()
        await finish.wait()
        return "held"

    async def short(name: str) -> str:
        order.append(name)
        return name

    await dispatcher.start()
    first = asyncio.create_task(
        dispatcher.submit("hold", "root-1", Resources(slots=1, memory_bytes=256), hold)
    )
    tasks = [first]
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        large = asyncio.create_task(
            dispatcher.submit(
                "large",
                "root-2",
                Resources(slots=1, memory_bytes=256),
                lambda: short("等待 256 的工具"),
            )
        )
        tasks.append(large)
        await asyncio.sleep(0)
        small = asyncio.create_task(
            dispatcher.submit(
                "small",
                "root-3",
                Resources(slots=1, memory_bytes=64),
                lambda: short("只需 64 的小工具"),
            )
        )
        tasks.append(small)
        await asyncio.wait_for(asyncio.shield(small), timeout=2)
        assert not large.done()
        remaining = dispatcher.pool.available.memory_bytes
        finish.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
        assert order.index("只需 64 的小工具") < order.index("等待 256 的工具")
        assert not dispatcher.pool.allocations
        return {
            "总内存单位": 384,
            "派发顺序": order,
            "长工具运行时小工具结束后的剩余内存": remaining,
            "结论": "可行小工具绕过暂不可行的大工具，所有分配最终归还。",
        }
    finally:
        finish.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await dispatcher.close()


CASES = {
    "aging": aging,
    "fairness": fairness,
    "banker": banker,
    "deadlock": deadlock,
    "resources": resources,
}
