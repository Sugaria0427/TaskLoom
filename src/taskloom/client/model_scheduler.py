"""L3 模型请求调度器：成本公平、期限老化、平台反馈协调。

:class:`ModelScheduler` 统管个人端的模型请求。它拿 :class:`AgingQueue`
在根任务之间做成本公平，派发前读一次平台反馈，再把
:class:`Decision` 翻译成提交、等待或拒绝。它是 :class:`Dispatcher`
在模型侧的对应物；两者不共享状态，因为工具槽位和模型在途上限是分开的。

三件事的边界必须清楚：

* **候选归属**：从队列取出到真正派发之间，请求属于调度器。任何一步
  失败都要么原样放回队列并保留它已经等待的时长，要么把异常交给调用者，
  不允许既吞掉异常又让调用者一直等。
* **并发名额**：主循环只负责挑人并原子占名额，实际执行放在独立任务里，
  这样 ``max_inflight`` 才有并行意义。
* **期限终态**：过了期限的请求是终态，从队列移除并以稳定错误码结束，
  不是简单地不再派发。

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

from taskloom.client.feedback_decision import Decision, decide
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
        coordinated: 为 ``True`` 时每次派发前读反馈；为 ``False`` 时
            直接提交，构成对照基线。
        quantum: 根任务之间成本轮转的额度。
        urgency_window: 距期限小于这个值算紧迫，触发提升。
        aging_window: 排队超过这个值算老化，触发提升。
        urgency_burst: 连续提升次数上限，防止紧急请求挤掉后台。
        feedback: 返回最新 :class:`PlatformFeedback` 的可调用对象，
            每次派发判定调用一次；缓存由调用方负责。
        now: 返回纪元秒的同步函数，默认 :func:`time.time`，测试可注入。

    不变量:
        * 在途请求数不超过 ``max_inflight``。
        * ``cancel`` 把请求移出队列并以 :class:`asyncio.CancelledError`
          结束它的 future。
        * ``close`` 取消排队、在途和执行任务，拒绝后续提交。
        * 取出后未派发的候选一定回到队列或交给调用者，不留悬挂请求。
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
        self._queued: set[str] = set()
        self._queued_at: dict[str, float] = {}
        self._feedback_failures: dict[str, int] = {}
        self._wake = asyncio.Event()
        self._loop: asyncio.Task[None] | None = None
        self._executions: set[asyncio.Task[None]] = set()

    @property
    def in_flight(self) -> int:
        """当前正在派发的请求数。"""
        return len(self._in_flight)

    @property
    def pending(self) -> int:
        """当前排队等待的请求数。"""
        return len(self._queue)

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
        self._queued.add(request.request_id)
        self._queued_at[request.request_id] = self._now()
        self._wake.set()
        try:
            return await pending.future
        except asyncio.CancelledError:
            self._drop(request.request_id)
            self._wake.set()
            raise

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
        """取消排队、在途请求和执行任务，然后停掉循环。"""
        if self._loop is not None:
            self._loop.cancel()
        executions = list(self._executions)
        for execution in executions:
            execution.cancel()
        for pending in list(self._by_id.values()) + list(self._in_flight.values()):
            if not pending.future.done():
                pending.future.cancel()
        self._by_id.clear()
        self._in_flight.clear()
        self._queued.clear()
        self._queued_at.clear()
        self._feedback_failures.clear()
        if self._loop is not None:
            await asyncio.gather(self._loop, return_exceptions=True)
        if executions:
            await asyncio.gather(*executions, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            try:
                await self._step()
            except asyncio.CancelledError:
                return

    async def _step(self) -> None:
        """跑一轮：收掉过期请求，挑候选，占名额，派发。"""
        self._retire_expired()
        candidates = self._queue.pop(eligible=self._eligible)
        if not candidates:
            await self._idle()
            return
        for pending in candidates:
            # 候选已经离开队列，成员标记要整体撤掉，否则放回时会被
            # 误判成还在排队而丢失。
            self._queued.discard(pending.request.request_id)
        for index, pending in enumerate(candidates):
            if self.in_flight >= self._max_inflight:
                self._requeue_all(candidates[index:])
                await self._await_wakeup()
                return
            try:
                feedback = await self._get_feedback()
            except Exception as error:
                self._requeue_all(candidates[index + 1 :])
                self._on_feedback_failure(pending, error)
                await asyncio.sleep(_FEEDBACK_RETRY_DELAY)
                return
            decision = self._decide(feedback, pending.request.reserve_units)
            if decision.action == "submit":
                self._launch(pending)
            elif decision.action == "wait":
                self._requeue_all(candidates[index:])
                await asyncio.sleep(decision.retry_after or _DEFAULT_RETRY_SECONDS)
                self._wake.set()
                return
            else:
                self._refuse(pending, decision.reason)

    async def _idle(self) -> None:
        """没候选时睡到有事发生，或者最早的那个期限到点。"""
        if self._queue.blocked():
            # 额度还没攒够，让出控制权再来一轮就够了，不睡死。
            await asyncio.sleep(0)
            return
        self._wake.clear()
        if self._queued:
            self._wake.set()  # 刚才这一刻有请求进来，不能睡过去
            return
        await self._await_wakeup()

    async def _await_wakeup(self) -> None:
        """等在途名额释放，或者队列里最早的那个期限到点。

        必须真的睡下去：名额只有执行任务收尾时才会释放，那种唤醒
        来自别的协程，循环自己不会转，一味同步空转会把事件循环饿死。
        """
        deadline = self._queue.next_deadline()
        timeout = None if deadline is None else max(0.0, deadline - self._now())
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

    def _eligible(self, pending: _Pending) -> bool:
        """只放行没被取消、也没过期的请求。"""
        return not pending.future.done()

    def _retire_expired(self) -> None:
        """把过期的请求收成终态：出队、清身份表、把错误交给调用者。"""
        cutoff = self._now()

        def expired(pending: _Pending) -> bool:
            return _deadline_epoch(pending.request, self._now) < cutoff

        for _, pending in self._queue.remove(expired):
            request_id = pending.request.request_id
            self._by_id.pop(request_id, None)
            self._queued.discard(request_id)
            self._queued_at.pop(request_id, None)
            self._feedback_failures.pop(request_id, None)
            if not pending.future.done():
                pending.future.set_exception(Refused(DEADLINE_EXPIRED))

    def _on_feedback_failure(self, pending: _Pending, error: Exception) -> None:
        """读反馈失败：先保留原等待时长放回队列，反复失败才交给调用者。

        重试有上限。反馈源坏掉时如果一直重试，请求就会永远挂着，
        调用者既拿不到结果也等不到错误，所以超过上限就清理干净并把
        原始异常交出去。
        """
        request_id = pending.request.request_id
        attempts = self._feedback_failures.get(request_id, 0) + 1
        if attempts < _FEEDBACK_RETRY_LIMIT:
            self._feedback_failures[request_id] = attempts
            self._requeue(pending)
            return
        self._drop(request_id)
        if not pending.future.done():
            pending.future.set_exception(error)

    def _decide(self, feedback: PlatformFeedback | None, reserve_units: int) -> Decision:
        if feedback is None:
            return Decision("submit", "no_feedback")
        return decide(
            feedback,
            now=self._now,
            in_flight=self.in_flight,
            max_inflight=self._max_inflight,
            coordinated=self._coordinated,
            has_queued=feedback.account_queued > 0,
            reserve_units=reserve_units,
        )

    def _launch(self, pending: _Pending) -> None:
        """原子占一个在途名额，把执行交给独立任务，主循环继续挑下一个。"""
        request_id = pending.request.request_id
        self._in_flight[request_id] = pending
        self._by_id.pop(request_id, None)
        self._queued.discard(request_id)
        self._queued_at.pop(request_id, None)
        self._feedback_failures.pop(request_id, None)
        execution = asyncio.create_task(self._execute(pending))
        self._executions.add(execution)
        execution.add_done_callback(self._executions.discard)

    async def _execute(self, pending: _Pending) -> None:
        """跑完一个请求的回调并把结果交给调用者。"""
        request_id = pending.request.request_id
        try:
            outcome: object = pending.callback(pending.request)
            if asyncio.iscoroutine(outcome):
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

    def _refuse(self, pending: _Pending, reason: str) -> None:
        request_id = pending.request.request_id
        self._by_id.pop(request_id, None)
        self._queued.discard(request_id)
        self._queued_at.pop(request_id, None)
        self._feedback_failures.pop(request_id, None)
        if not pending.future.done():
            pending.future.set_exception(Refused(reason))
        self._wake.set()

    def _requeue_all(self, pending_list: list[_Pending]) -> None:
        for pending in pending_list:
            self._requeue(pending)

    def _requeue(self, pending: _Pending) -> None:
        """放回队列，保留它原来的入队时刻，老化时钟不会清零。

        已经在队列里的条目直接跳过，防止反复放回产生重复副本。
        """
        request_id = pending.request.request_id
        if pending.future.done() or request_id in self._queued:
            return
        self._queued.add(request_id)
        enqueued_at = self._queued_at.setdefault(request_id, self._now())
        self._queue.requeue(pending.request.root_task_id, pending, enqueued_at)

    def _track(self, pending: _Pending) -> None:
        self._by_id[pending.request.request_id] = pending

    def _drop(self, request_id: str) -> None:
        """从队列和身份表里彻底移除一个请求。"""
        self._queue.remove(lambda item: item.request.request_id == request_id)
        self._by_id.pop(request_id, None)
        self._queued.discard(request_id)
        self._queued_at.pop(request_id, None)
        self._feedback_failures.pop(request_id, None)


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
