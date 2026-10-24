"""L3 调度器的平台反馈来源：按周期复用快照，读失败就直说。

:class:`ModelScheduler` 每挑一个候选就问一次"现在能不能提交"。直接问网关
会把一次派发放大成一次网络往返，队列里候选多的时候尤其明显。这里按
:class:`ClientConfig` 的 ``feedback_interval_seconds`` 复用上一份快照。

三条约束不能破：

* **不改观测时间**。``observed_at`` 是平台写下这份反馈的时刻，复用它就得
  原样保留；改成本地读取时刻会让 :func:`decide` 的陈旧判定永远认为快照
  是新的。
* **读失败不拿旧快照顶替**。宁可把异常抛给调度器让它按自己的重试策略
  处理，也不要假装平台还在正常应答——那会让请求一直等在一个已经不成立的
  准入结论上。
* **只读**。不写平台状态，不动预算。

复用时长还要收在 :data:`STALE_FEEDBACK_SECONDS` 以内：配置允许把周期放宽到
三十秒，而一份超过陈旧阈值的快照在 :func:`decide` 眼里就该重读了。若不收窄，
调度器会反复拿到同一份自己判为陈旧的快照，请求一直卡到缓存过期才恢复。
"""

import time
from collections.abc import Awaitable, Callable

from taskloom.client.feedback_decision import STALE_FEEDBACK_SECONDS
from taskloom.contracts.models import PlatformFeedback

FeedbackReader = Callable[[], Awaitable[PlatformFeedback]]


class FeedbackSource:
    """带周期缓存的反馈读取器。

    缓存只是省掉重复往返，判断依据永远是缓存里那份原始反馈：它够旧时，
    :func:`decide` 自己会返回 ``stale_feedback`` 让调度器重读。

    Args:
        read: 向平台读取反馈的协程函数。
        interval_seconds: 快照复用时长，必须为正；实际生效值不超过
            :data:`STALE_FEEDBACK_SECONDS`。
        now: 取当前时刻的函数，测试可替换。
    """

    def __init__(
        self,
        read: FeedbackReader,
        interval_seconds: float,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("Feedback interval must be positive")
        self._read = read
        self._interval = min(interval_seconds, STALE_FEEDBACK_SECONDS)
        self._now = now
        self._cached: PlatformFeedback | None = None
        self._fetched_at = float("-inf")

    @property
    def cached(self) -> PlatformFeedback | None:
        """当前可复用的快照，没有可用快照时为 ``None``。"""
        if self._cached is None or self._now() - self._fetched_at >= self._interval:
            return None
        return self._cached

    @property
    def latest(self) -> PlatformFeedback | None:
        """最近一次读到的快照，过期了也照样返回。

        只给状态汇报用：面板要让人看见调度器当下依据的是哪一份，而
        :func:`decide` 的新鲜度判定必须走 :attr:`cached`，否则一份过期
        快照会被当成可用依据。这里不做任何过滤，调用方自己判断要不要信。
        """
        return self._cached

    async def __call__(self) -> PlatformFeedback:
        """返回一份反馈；缓存还在有效期内就直接复用，否则重新读取。"""
        reusable = self.cached
        if reusable is not None:
            return reusable
        feedback = await self._read()
        self._cached = feedback
        self._fetched_at = self._now()
        return feedback

    def invalidate(self) -> None:
        """丢弃缓存，下一次读取强制回平台。"""
        self._cached = None
        self._fetched_at = float("-inf")
