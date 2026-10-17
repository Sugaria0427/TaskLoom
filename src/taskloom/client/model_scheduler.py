"""L3 model-request scheduler: cost DRR + aging + feedback coordination.

:class:`ModelScheduler` orchestrates personal-side model requests. It
owns an :class:`AgingQueue` for fairness among root tasks, queries the
platform for a feedback snapshot before each dispatch, and translates
the :class:`Decision` into submit/wait/refuse actions. The scheduler is
the L3 counterpart of :class:`Dispatcher`, which serves L1 tools; the
two never share state because the WP3 design keeps tool slots and
model slots distinct (``ClientConfig.tool_slots`` vs
``ClientConfig.max_inflight``).

The scheduler is asynchronous and follows the same lifecycle as
:class:`Dispatcher`: ``start`` launches a background loop, ``submit``
returns the gateway record when the request is served, ``cancel``
removes a pending request, and ``close`` drains the queue.

Example:
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
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast

from taskloom.client.feedback_decision import Decision, decide
from taskloom.contracts.models import GatewayRecord, ModelRequest, PlatformFeedback
from taskloom.policies.aging import AgingQueue


class Refused(Exception):
    """Raised when :class:`ModelScheduler` refuses a request.

    Attributes:
        reason: Stable string code from :class:`Decision.reason`.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class _Pending:
    request: ModelRequest
    callback: Callable[[ModelRequest], Awaitable[GatewayRecord]]
    future: asyncio.Future[GatewayRecord]


class ModelScheduler:
    """Cost-DRR + feedback-aware L3 scheduler.

    Args:
        max_inflight: Local cap on simultaneous in-flight requests
            (``ClientConfig.max_inflight``). Hard-stops more concurrent
            dispatches even if the platform would allow them.
        coordinated: When ``True``, the scheduler reads feedback
            before each dispatch. When ``False``, the local ablation
            baseline submits immediately (caller's budget checks
            still apply).
        quantum: Cost-DRR quantum for fairness among root tasks.
        urgency_window: Seconds-from-deadline that trigger urgency
            promotion. See :class:`AgingQueue`.
        aging_window: Seconds-in-queue that trigger aging promotion.
        feedback: Async callable returning the latest
            :class:`PlatformFeedback`. Called once per dispatch
            decision; cached results are the caller's responsibility.
        now: Sync callable returning epoch seconds. Defaults to
            :func:`time.time`; injected for tests.

    Invariants:
        * In-flight requests never exceed ``max_inflight``.
        * Cancel removes the request from the queue and resolves its
          future with :class:`asyncio.CancelledError`.
        * ``close`` drains pending futures with cancellation and
          refuses no further submissions.
    """

    def __init__(
        self,
        *,
        max_inflight: int,
        coordinated: bool,
        quantum: int,
        urgency_window: float,
        aging_window: float,
        feedback: Callable[[], PlatformFeedback] | Callable[[], Awaitable[PlatformFeedback]] | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        if max_inflight < 1:
            raise ValueError("max_inflight must be >= 1")
        self._max_inflight = max_inflight
        self._coordinated = coordinated
        self._quantum = quantum
        self._urgency_window = urgency_window
        self._aging_window = aging_window
        self._feedback: Callable[[], PlatformFeedback] | Callable[[], Awaitable[PlatformFeedback]] | None = feedback
        self._now = now
        self._queue: AgingQueue[_Pending] = AgingQueue(
            quantum=quantum,
            cost=lambda p: max(1, p.request.reserve_units),
            deadline=lambda p: _deadline_epoch(p.request, self._now),
            now=self._now,
            urgency_window=urgency_window,
            aging_window=aging_window,
        )
        self._by_id: dict[str, _Pending] = {}
        self._in_flight: dict[str, _Pending] = {}
        self._wake = asyncio.Event()
        self._loop: asyncio.Task[None] | None = None
        self._stopping = False

    @property
    def in_flight(self) -> int:
        """Current number of requests actively being dispatched."""
        return len(self._in_flight)

    async def start(self) -> None:
        """Launch the background dispatch loop."""
        self._loop = asyncio.create_task(self._run())

    async def submit(
        self,
        request: ModelRequest,
        callback: Callable[[ModelRequest], Awaitable[GatewayRecord]],
    ) -> GatewayRecord:
        """Submit ``request`` and await its gateway record.

        Args:
            request: The :class:`ModelRequest` to dispatch.
            callback: Async callable that performs the actual gateway
                call (e.g. ``GatewayPort.submit``). The scheduler
                invokes it exactly once per non-refused request.

        Returns:
            The :class:`GatewayRecord` produced by ``callback``.

        Raises:
            Refused: When the feedback-decision refuses the request
                (e.g. insufficient budget).
            asyncio.CancelledError: When cancelled before dispatch.
        """
        if self._loop is None:
            raise RuntimeError("ModelScheduler.start must be called first")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[GatewayRecord] = loop.create_future()
        pending = _Pending(request=request, callback=callback, future=future)
        self._by_id[request.request_id] = pending
        self._queue.push(request.root_task_id, pending)
        self._wake.set()
        try:
            return await future
        except asyncio.CancelledError:
            self._queue.remove(lambda p: p.request.request_id == request.request_id)
            self._by_id.pop(request.request_id, None)
            self._wake.set()
            raise

    async def cancel(self, request_id: str) -> bool:
        """Cancel a pending request.

        Returns:
            ``True`` if the request was found and cancelled, ``False``
            if it had already been dispatched or never existed.
        """
        pending = self._by_id.get(request_id)
        if pending is None:
            return False
        self._queue.remove(lambda p: p.request.request_id == request_id)
        if not pending.future.done():
            pending.future.cancel()
        self._by_id.pop(request_id, None)
        self._wake.set()
        return True

    async def close(self) -> None:
        """Cancel pending and in-flight requests, then stop the loop.

        Both queued requests (still in :attr:`_by_id`) and in-flight
        callbacks receive a cancel. Use :meth:`cancel` for finer
        control over individual requests.
        """
        if self._loop is not None:
            self._loop.cancel()
        for pending in list(self._by_id.values()) + list(self._in_flight.values()):
            if not pending.future.done():
                pending.future.cancel()
        self._by_id.clear()
        self._in_flight.clear()
        if self._loop is not None:
            await asyncio.gather(self._loop, return_exceptions=True)

    async def _get_feedback(self) -> PlatformFeedback | None:
        feedback = self._feedback
        if feedback is None:
            return None
        if asyncio.iscoroutinefunction(feedback):
            value = await feedback()
            return cast(PlatformFeedback, value)
        value = feedback()
        return cast(PlatformFeedback, value)

    def _eligible(self, pending: _Pending) -> bool:
        """Skip expired or already-cancelled requests."""
        if pending.future.cancelled():
            return False
        deadline_epoch = _deadline_epoch(pending.request, self._now)
        return self._now() <= deadline_epoch

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self._step()
            except asyncio.CancelledError:
                return
            except Exception:
                # Keep the loop alive on transient scheduler errors.
                self._wake.set()

    async def _step(self) -> None:
        if self._loop is None:
            return
        if self.in_flight >= self._max_inflight:
            await self._wait()
            return
        pending = self._queue.pop(eligible=self._eligible)
        if pending is None:
            await self._wait()
            return
        feedback = await self._get_feedback()
        decision = self._decide(feedback, pending.request.reserve_units)
        if decision.action == "submit":
            await self._dispatch(pending)
        elif decision.action == "wait":
            self._queue.push(pending.request.root_task_id, pending)
            await asyncio.sleep(decision.retry_after or 0.1)
            self._wake.set()
        else:
            self._by_id.pop(pending.request.request_id, None)
            if not pending.future.done():
                pending.future.set_exception(Refused(decision.reason))

    def _decide(self, feedback: PlatformFeedback | None, reserve_units: int) -> Decision:
        if feedback is None:
            return Decision("submit", "no_feedback")
        has_queued = feedback.account_queued > 0
        return decide(
            feedback,
            now=self._now,
            in_flight=self.in_flight,
            max_inflight=self._max_inflight,
            coordinated=self._coordinated,
            has_queued=has_queued,
            reserve_units=reserve_units,
        )

    async def _dispatch(self, pending: _Pending) -> None:
        request_id = pending.request.request_id
        self._in_flight[request_id] = pending
        self._by_id.pop(request_id, None)
        try:
            outcome = pending.callback(pending.request)
            if asyncio.iscoroutine(outcome):
                record = await outcome
            else:
                record = outcome
        except asyncio.CancelledError:
            if not pending.future.done():
                pending.future.cancel()
            raise
        except Exception as error:
            if not pending.future.done():
                pending.future.set_exception(error)
            return
        finally:
            self._in_flight.pop(request_id, None)
            self._wake.set()
        if not pending.future.done():
            pending.future.set_result(record)

    async def _wait(self) -> None:
        self._wake.clear()
        await self._wake.wait()


def _deadline_epoch(request: ModelRequest, now: Callable[[], float]) -> float:
    """Absolute deadline as a float epoch.

    Requests whose ``deadline_at`` cannot be parsed are treated as
    far-future so they never trigger urgency promotion.
    """
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(request.deadline_at)
    except ValueError:
        return now() + 1e9
    if parsed.tzinfo is None:
        return now() + 1e9
    return parsed.timestamp()