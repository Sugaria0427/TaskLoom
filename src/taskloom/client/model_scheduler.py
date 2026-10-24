"""L3 模型请求调度器：成本公平、期限老化、平台反馈协调。

:class:`ModelScheduler` 统管个人端的模型请求。它拿 :class:`AgingQueue`
在根任务之间做成本公平，派发前读一次平台反馈，再把判定翻译成提交、
等待或拒绝。它是 :class:`Dispatcher` 在模型侧的对应物；两者不共享状态，
因为工具槽位和模型在途上限是分开的。

四件事的边界必须清楚：

* **取出即派发**：队列按"当前还剩几个在途名额"取候选，并且只取反馈
  认可的条目。取出来的就是要派发的，不再放回——放回会重复记账、重复
  提升，候选列表里的公平顺序也没落实到实际派发顺序上。
* **并发名额**：主循环只负责挑人并原子占名额，实际执行放在独立任务里，
  这样 ``max_inflight`` 才有并行意义。
* **期限终态**：过了期限的请求是终态，从队列移除并以稳定错误码结束，
  不是简单地不再派发。任何退避都要被最早的那个期限打断。
* **取消传递**：调用者放弃等待时，取消要传到真正打网关的那个任务上，
  并等它把平台侧取消交接完；否则平台取消发不出去，在途名额和预留也
  收不回来。

示例:
    >>> import asyncio
    >>> async def main() -> None:
    ...     sched = ModelScheduler(
    ...         max_inflight=1, coordinated=False, quantum=100,
    ...         urgency_window=60.0, aging_window=10.0,
    ...     )
    ...     await sched.start()
    ...     await sched.close()
    >>> asyncio.run(main())
"""

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast

from taskloom.client.feedback_decision import (
    account_decision,
    request_decision,
)
from taskloom.contracts.models import GatewayRecord, ModelRequest, PlatformFeedback
from taskloom.policies.aging import AgingQueue

_DEFAULT_RETRY_SECONDS = 0.1
_FEEDBACK_RETRY_LIMIT = 3
_FEEDBACK_RETRY_DELAY = 0.05
DEADLINE_EXPIRED = "deadline_expired"


class Refused(Exception):
    """调度器拒绝某个请求时抛出。

    属性:
        reason: 来自 :class:`Decision.reason` 的稳定错误码。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class _Pending:
    """一个已登记、尚未完成的请求。"""

    request: ModelRequest
    callback: Callable[[ModelRequest], Awaitable[GatewayRecord]]
    future: asyncio.Future[GatewayRecord]


class ModelScheduler:
    """成本公平加反馈感知的 L3 调度器。

    参数:
        max_inflight: 本地在途上限，硬性截断并发派发数量。
        coordinated: 为 ``True`` 时按反馈判定派发；为 ``False`` 时直接
            提交，构成对照基线。
        quantum: 根任务之间成本轮转的额度。
        urgency_window: 距期限小于这个值算紧迫，触发提升。
        aging_window: 排队超过这个值算老化，触发提升。
        urgency_burst: 连续提升次数上限，防止紧急请求挤掉后台。
        feedback: 返回最新 :class:`PlatformFeedback` 的可调用对象，
            每轮读一次；缓存由调用方负责。
        now: 返回纪元秒的同步函数，默认 :func:`time.time`，测试可注入。

    不变量:
        * 在途请求数不超过 ``max_inflight``。
        * ``cancel`` 把请求移出队列并以 :class:`asyncio.CancelledError`
          结束它的 future。
        * ``close`` 取消排队、在途和执行任务，拒绝后续提交。
        * 从队列取出的条目一定会被派发，不再放回队列。
    """

    def __init__(
        self,
        *,
        max_inflight: int,
        coordinated: bool,
        quantum: int,
        urgency_window: float,
        aging_window: float,
        urgency_burst: int = 2,
        feedback: (
            Callable[[], PlatformFeedback] | Callable[[], Awaitable[PlatformFeedback]] | None
        ) = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        if max_inflight < 1:
            raise ValueError("max_inflight must be >= 1")
        self._max_inflight = max_inflight
        self._coordinated = coordinated
        self._urgency_window = urgency_window
        self._aging_window = aging_window
        self._urgency_burst = urgency_burst
        provider: (
            Callable[[], PlatformFeedback] | Callable[[], Awaitable[PlatformFeedback]] | None
        ) = feedback
        self._feedback = provider
        self._now = now
        self._queue: AgingQueue[_Pending] = AgingQueue(
            quantum=quantum,
            cost=lambda pending: max(1, pending.request.reserve_units),
            deadline=lambda pending: _deadline_epoch(pending.request, self._now),
            now=self._now,
            urgency_window=urgency_window,
            aging_window=aging_window,
            urgency_burst=urgency_burst,
        )
        self._by_id: dict[str, _Pending] = {}
        self._in_flight: dict[str, _Pending] = {}
        self._feedback_failures = 0
        self._wake = asyncio.Event()
        self._loop: asyncio.Task[None] | None = None
        self._executions: dict[str, asyncio.Task[None]] = {}

    @property
    def in_flight(self) -> int:
        """当前正在派发的请求数。"""
        return len(self._in_flight)

    @property
    def pending(self) -> int:
        """当前排队等待的请求数。"""
        return len(self._queue)

    @property
    def in_flight_requests(self) -> tuple[tuple[str, str], ...]:
        """当前在途请求的 ``(request_id, root_task_id)`` 快照。

        供客户端汇报资源占用用。不返回内部对象，调用方改不动在途请求。
        """
        return tuple(
            (pending.request.request_id, pending.request.root_task_id)
            for pending in self._in_flight.values()
        )

    async def start(self) -> None:
        """启动后台调度循环。"""
        self._loop = asyncio.create_task(self._run())

    async def submit(
        self,
        request: ModelRequest,
        callback: Callable[[ModelRequest], Awaitable[GatewayRecord]],
    ) -> GatewayRecord:
        """提交 ``request`` 并等它的网关记录。

        参数:
            request: 要派发的 :class:`ModelRequest`。
            callback: 真正打网关的可调用对象，调度器对每个未被拒绝的
                请求恰好调用一次。

        返回:
            ``callback`` 产出的 :class:`GatewayRecord`。

        异常:
            Refused: 反馈判定拒绝时抛出，原因见 ``.reason``；期限过期
                时原因为 ``deadline_expired``。
            asyncio.CancelledError: 派发前被取消时抛出。
        """
        if self._loop is None:
            raise RuntimeError("ModelScheduler.start must be called first")
        loop = asyncio.get_running_loop()
        pending = _Pending(
            request=request,
            callback=callback,
            future=loop.create_future(),
        )
        self._track(pending)
        self._queue.push(request.root_task_id, pending)
        self._wake.set()
        try:
            return await pending.future
        except asyncio.CancelledError:
            await self._abandon(request.request_id)
            raise

    def settle(self, owner: str, reserved: int, consumed: int) -> None:
        """按实际消费校正 ``owner`` 的公平账本。

        派发时按 :attr:`ModelRequest.reserve_units` 预扣，那只是估算。
        结算后多扣的退回、少扣的补记，否则预留偏高的请求会被长期惩罚，
        预留偏低的那一方则能占便宜。

        Args:
            owner: 根任务标识，与提交时的 ``root_task_id`` 一致。
            reserved: 派发时预扣的额度。
            consumed: 实际消费；``0`` 且结果未知时不要调用本方法。
        """
        if reserved < 0 or consumed < 0:
            raise ValueError("Settled units must be non-negative")
        self._queue.refund(owner, reserved - consumed)

    async def cancel(self, request_id: str) -> bool:
        """取消一个尚未派发的请求。

        返回:
            找到并取消返回 ``True``；已经派发或压根不存在返回 ``False``。
        """
        pending = self._by_id.get(request_id)
        if pending is None or request_id in self._in_flight:
            return False
        self._drop(request_id)
        if not pending.future.done():
            pending.future.cancel()
        self._wake.set()
        return True

    async def close(self) -> None:
        """取消排队、在途请求和执行任务，然后停掉循环。

        循环指针会一并清掉，之后的 :meth:`submit` 直接被拒；不清的话
        :meth:`submit` 的启动检查会一直放行，而循环已经死了，调用者
        只会永久挂在 future 上。
        """
        loop_task = self._loop
        if loop_task is not None:
            loop_task.cancel()
        self._loop = None
        executions = list(self._executions.values())
        for execution in executions:
            execution.cancel()
        for pending in list(self._by_id.values()) + list(self._in_flight.values()):
            if not pending.future.done():
                pending.future.cancel()
        self._by_id.clear()
        self._in_flight.clear()
        self._executions.clear()
        self._feedback_failures = 0
        if loop_task is not None:
            await asyncio.gather(loop_task, return_exceptions=True)
        if executions:
            await asyncio.gather(*executions, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            try:
                await self._step()
            except asyncio.CancelledError:
                return

    async def _step(self) -> None:
        """跑一轮：收掉过期请求，读反馈，按剩余名额取候选并派发。"""
        self._retire_expired()
        capacity = self._max_inflight - self.in_flight
        if capacity <= 0:
            await self._await_wakeup()
            return
        if len(self._queue) == 0:
            await self._idle()
            return
        if not self._queue.ready(self._eligible):
            # 额度还没攒够：先让轮转把额度发出来。这一步既不读平台也不
            # 派发，只推进轮转环；否则每一轮都会白读一次平台反馈。
            self._queue.pop(eligible=self._eligible, limit=capacity, accept=lambda _: False)
            await asyncio.sleep(0)
            return
        try:
            feedback = await self._get_feedback()
        except Exception as error:
            await self._on_feedback_failure(error)
            return
        self._feedback_failures = 0
        accept: Callable[[_Pending], bool] | None = None
        if feedback is not None and self._coordinated:
            account = account_decision(
                feedback,
                now=self._now,
                in_flight=self.in_flight,
                max_inflight=self._max_inflight,
                coordinated=self._coordinated,
                has_queued=feedback.account_queued > 0,
            )
            if account.reason == "stale_feedback":
                # 快照不可信时不做任何终态判定，只退避等下一次读数。
                await self._await_wakeup(account.retry_after)
                return
            self._refuse_unaffordable(feedback)
            if account.action == "wait":
                await self._await_wakeup(account.retry_after)
                return
            accept = self._acceptor(feedback)
        rejected = False
        candidates: list[_Pending] = []
        if accept is None:
            candidates = self._queue.pop(eligible=self._eligible, limit=capacity)
        else:

            def accept_tracking(pending: _Pending) -> bool:
                nonlocal rejected
                if accept(pending):
                    return True
                rejected = True
                return False

            candidates = self._queue.pop(
                eligible=self._eligible,
                limit=capacity,
                accept=accept_tracking,
            )
        for pending in candidates:
            self._launch(pending)
        if candidates:
            return
        if rejected:
            # 请求级阻塞：等平台额度或最早期限，不空转。
            retry_after = feedback.retry_after_seconds if feedback is not None else 0.0
            await self._await_wakeup(retry_after)
            return
        await self._idle()

    async def _idle(self) -> None:
        """没候选时睡到有事发生，或者最早的那个期限到点。"""
        if self._queue.blocked():
            # 额度还没攒够，让出控制权再来一轮就够了，不睡死。
            await asyncio.sleep(0)
            return
        if len(self._queue) > 0:
            self._wake.set()  # 刚才这一刻有请求进来，不能睡过去
            return
        await self._await_wakeup()

    async def _await_wakeup(self, backoff: float | None = None) -> None:
        """等在途名额释放、有事发生，或者最早的那个期限到点。

        ``backoff`` 是调用方给的退避上限；实际等待取它与"距最近期限还有
        多久"的较小值，所以请求到期不会被一段长退避挡住。

        必须真的睡下去：名额只有执行任务收尾时才会释放，那种唤醒
        来自别的协程，循环自己不会转，一味同步空转会把事件循环饿死。
        """
        deadline = self._queue.next_deadline()
        until_deadline = None if deadline is None else max(0.0, deadline - self._now())
        timeout = backoff
        if timeout is None:
            timeout = until_deadline
        elif until_deadline is not None:
            timeout = min(timeout, until_deadline)
        if timeout is not None and timeout <= 0:
            timeout = 0
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout)
        except TimeoutError:
            self._wake.set()

    async def _get_feedback(self) -> PlatformFeedback | None:
        """读一次反馈，按返回值决定要不要 await。

        按返回值判断而不是按函数类型判断：同步函数返回 awaitable 也是
        合法接口（``functools.partial``、闭包包装都可能是这样），按类型
        判断会把没 await 的协程当成反馈对象用。
        """
        provider = self._feedback
        if provider is None:
            return None
        value = provider()
        if inspect.isawaitable(value):
            return await value
        return value

    def _acceptor(self, feedback: PlatformFeedback) -> Callable[[_Pending], bool]:
        """构造请求级准入谓词：只放行自己能付得起的那一条。

        被挡住的那一条留在队列原位，等待时长与次序都不变；同一轮里
        别的根任务照常派发，不会被一条凑不齐额度的大请求整体拖住。
        """

        def accept(pending: _Pending) -> bool:
            decision = request_decision(feedback, reserve_units=pending.request.reserve_units)
            return decision.action == "submit"

        return accept

    def _refuse_unaffordable(self, feedback: PlatformFeedback) -> None:
        """把账户余额已经付不起的请求收成终态。

        这一步必须排在账户级退避之前：预算耗尽时平台同样把
        ``can_submit`` 置成 ``False``，先看账户级结论会让这些请求
        白等下去。
        """

        def unaffordable(pending: _Pending) -> bool:
            decision = request_decision(feedback, reserve_units=pending.request.reserve_units)
            return decision.action == "refuse"

        for _, pending in self._queue.remove(unaffordable):
            self._refuse(pending, "insufficient_budget")

    def _eligible(self, pending: _Pending) -> bool:
        """只放行没被取消、也没过期的请求。"""
        return not pending.future.done()

    def _retire_expired(self) -> None:
        """把过期的请求收成终态：出队、清身份表、把错误交给调用者。"""
        cutoff = self._now()

        def expired(pending: _Pending) -> bool:
            return _deadline_epoch(pending.request, self._now) < cutoff

        for owner, pending in self._queue.remove(expired):
            del owner
            request_id = pending.request.request_id
            self._by_id.pop(request_id, None)
            if not pending.future.done():
                pending.future.set_exception(Refused(DEADLINE_EXPIRED))

    async def _on_feedback_failure(self, error: Exception) -> None:
        """读反馈失败：先重试，反复失败才把原始异常交给所有等待者。

        重试有上限。反馈源坏掉时如果一直重试，请求就会永远挂着，
        调用者既拿不到结果也等不到错误，所以超过上限就清理干净并把
        原始异常交出去。
        """
        self._feedback_failures += 1
        if self._feedback_failures < _FEEDBACK_RETRY_LIMIT:
            await asyncio.sleep(_FEEDBACK_RETRY_DELAY)
            return
        self._feedback_failures = 0
        for pending in list(self._by_id.values()):
            if not pending.future.done():
                pending.future.set_exception(error)
        self._by_id.clear()
        self._queue.remove(lambda _: True)

    def _launch(self, pending: _Pending) -> None:
        """原子占一个在途名额，把执行交给独立任务，主循环继续挑下一个。"""
        request_id = pending.request.request_id
        self._in_flight[request_id] = pending
        self._by_id.pop(request_id, None)
        execution = asyncio.create_task(self._execute(pending))
        self._executions[request_id] = execution
        execution.add_done_callback(lambda _: self._executions.pop(request_id, None))

    async def _execute(self, pending: _Pending) -> None:
        """跑完一个请求的回调并把结果交给调用者。"""
        request_id = pending.request.request_id
        try:
            outcome: object = pending.callback(pending.request)
            if inspect.isawaitable(outcome):
                record = await cast(Awaitable[GatewayRecord], outcome)
            else:
                record = cast(GatewayRecord, outcome)
        except asyncio.CancelledError:
            if not pending.future.done():
                pending.future.cancel()
            raise
        except Exception as error:
            if not pending.future.done():
                pending.future.set_exception(error)
        else:
            if not pending.future.done():
                pending.future.set_result(record)
        finally:
            self._in_flight.pop(request_id, None)
            self._wake.set()

    async def _abandon(self, request_id: str) -> None:
        """调用者放弃等待：把取消传给执行任务，并等它收尾。

        已经派发的请求如果只出队不取消执行任务，网关那边的取消分支
        永远不会执行：平台收不到取消、在途名额与预留都收不回来。
        还在排队的请求直接出队就够了。
        """
        execution = self._executions.get(request_id)
        if execution is not None and not execution.done():
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
        else:
            self._drop(request_id)
        self._wake.set()

    def _refuse(self, pending: _Pending, reason: str) -> None:
        request_id = pending.request.request_id
        self._by_id.pop(request_id, None)
        if not pending.future.done():
            pending.future.set_exception(Refused(reason))
        self._wake.set()

    def _track(self, pending: _Pending) -> None:
        self._by_id[pending.request.request_id] = pending

    def _drop(self, request_id: str) -> None:
        """从队列和身份表里彻底移除一个请求。"""
        self._queue.remove(lambda item: item.request.request_id == request_id)
        self._by_id.pop(request_id, None)


def _deadline_epoch(request: ModelRequest, now: Callable[[], float]) -> float:
    """把 ``deadline_at`` 解析成浮点纪元秒。

    解析不了或者没有时区的，按很远将来处理，这样它们永远不会触发
    紧迫提升，也永远不会被判过期。
    """
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(request.deadline_at)
    except ValueError:
        return now() + 1e9
    if parsed.tzinfo is None:
        return now() + 1e9
    return parsed.timestamp()
