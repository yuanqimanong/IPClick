"""按 host 的异步并发与速率限制（0.7.0）。

和同步的 :class:`~ipclick.limiter.HostLimiter` 共用一份配置
（:class:`~ipclick.limiter.LimiterSettings`），但内部换成 asyncio 原语。

**为什么不能直接复用同步那个**：它的等待是 ``threading.Semaphore.acquire``
和 ``time.sleep``。在协程里 ``time.sleep`` 阻塞的不是这一个请求，而是**整个
事件循环**——同一个循环上所有在飞的请求一起停住。丢进线程池能绕开阻塞，
但每个等待中的请求就占着一个线程，协程省下来的那部分又还回去了。

令牌桶换成了**预约制**：每个请求到达时原子地从 ``next_available`` 上"预定"
一个未来时刻，然后精确睡到那一刻。

    t = max(now, next_available)
    next_available = t + 1/qps

发出时刻于是按到达顺序排成间隔恰好 ``1/qps`` 的等差数列——先到先服务，
不需要轮询。突发额度体现为允许 ``next_available`` 落后于 ``now`` 最多
``burst/qps`` 秒。

**关于精度，实测数据（600 并发、配 200 QPS）**：

    同步 600 线程   192.1 QPS   -4.0%
    异步 600 协程   200.5 QPS   +0.2%

同步版并不像"轮询实现"那样粗糙——它算的是 ``wait = (1 - tokens) / qps``，
是精确等待。它的 4% 误差来自**线程调度**：600 个线程被唤醒的时刻本身就有
抖动，且方向是**欠发**（慢于配置值），不是超发。

所以异步版换算法买到的精度提升是有限的（4% → 0.2%）。**真正的收益在别处**：
600 个协程 vs 600 个线程的内存与调度开销，以及不占线程、不阻塞事件循环。
"""

import asyncio
from collections.abc import AsyncIterator
import contextlib
from dataclasses import dataclass, field
import time

from ipclick.limiter import HostLimitTimeout, LimiterSettings, host_of
from ipclick.utils.log_util import log


@dataclass
class _AsyncSlot:
    """一个 host 的额度。"""

    semaphore: asyncio.Semaphore | None
    #: 下一个请求最早能在什么时刻发出（``time.monotonic()`` 时间轴）
    next_available: float = 0.0
    active: int = 0
    waiting: int = 0
    last_used: float = field(default_factory=time.monotonic)
    #: 保护 next_available 的原子预约。用 asyncio.Lock 而不是 threading.Lock：
    #: 后者在协程里持有期间无法让出，等于把并发退化成串行。
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class AsyncHostLimiter:
    """按 host 的异步限流闸门。未配置任何限额时是零开销的空操作。"""

    def __init__(self, settings: LimiterSettings | None = None) -> None:
        self.settings: LimiterSettings = settings or LimiterSettings()
        self._slots: dict[str, _AsyncSlot] = {}
        self._slots_lock: asyncio.Lock = asyncio.Lock()

    def __len__(self) -> int:
        return len(self._slots)

    @contextlib.asynccontextmanager
    async def acquire(self, url: str, timeout: float | None = None) -> AsyncIterator[None]:
        """取得该 URL 所属 host 的额度。用完自动归还。

        取的顺序与同步版一致：**先并发槽、后令牌**。并发槽是要保证的硬上限，
        先拿住；令牌紧挨着真正的请求再取，这样"每秒 N 个"限的是真实发出去的
        请求，而不是"进入排队的请求"。
        """
        settings = self.settings
        if not settings.enabled:
            yield
            return

        host = host_of(url)
        if not host:
            yield
            return

        slot = await self._slot_for(host)
        deadline = time.monotonic() + (settings.wait_timeout if timeout is None else max(0.0, timeout))

        acquired = await self._acquire_concurrency(slot, host, deadline)
        try:
            await self._acquire_token(slot, host, deadline)
            yield
        finally:
            slot.active -= 1
            slot.last_used = time.monotonic()
            if acquired and slot.semaphore is not None:
                slot.semaphore.release()

    async def _acquire_concurrency(self, slot: _AsyncSlot, host: str, deadline: float) -> bool:
        if slot.semaphore is None:
            slot.active += 1
            return False

        slot.waiting += 1
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            await asyncio.wait_for(slot.semaphore.acquire(), timeout=remaining)
        except TimeoutError as e:
            raise HostLimitTimeout(
                f"等待 {host} 的并发额度超时（上限 {self.settings.per_host_max_concurrent} 个并发，"
                f"已等待 {self.settings.wait_timeout:.1f} 秒）"
            ) from e
        finally:
            slot.waiting -= 1

        slot.active += 1
        return True

    async def _acquire_token(self, slot: _AsyncSlot, host: str, deadline: float) -> None:
        """预约式令牌桶：原子地占住一个未来时刻，然后精确睡到那一刻。

        相比同步版的"算出还差多久、睡那么久"，这里的差别在于**预约是原子的**：
        等待者一进来就把自己的发出时刻定死并写回 ``next_available``，之后只是
        睡到那一刻，不再重新竞争。于是顺序稳定（先到先服务）、也不存在多个
        等待者被同一次令牌补充同时唤醒的情况。

        实测 600 并发下误差 +0.2%，同步线程版是 -4.0%（欠发，来自线程调度抖动）。
        """
        qps = self.settings.per_host_qps
        if qps <= 0:
            return

        interval = 1.0 / qps
        # 突发额度：允许 next_available 落后于当前时刻最多 burst 个间隔，
        # 也就是攒下 burst 个令牌。
        max_lag = max(0.0, float(self.settings.burst)) * interval

        async with slot.lock:
            now = time.monotonic()
            earliest = now - max_lag
            scheduled = max(slot.next_available, earliest)
            if scheduled > deadline:
                raise HostLimitTimeout(
                    f"等待 {host} 的速率额度超时（上限 {qps:g} QPS，已等待 {self.settings.wait_timeout:.1f} 秒）"
                )
            slot.next_available = scheduled + interval

        wait = scheduled - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)

    async def _slot_for(self, host: str) -> _AsyncSlot:
        slot = self._slots.get(host)
        if slot is not None:
            slot.last_used = time.monotonic()
            return slot

        async with self._slots_lock:
            slot = self._slots.get(host)
            if slot is None:
                self._sweep_locked()
                slot = _AsyncSlot(
                    semaphore=(
                        asyncio.Semaphore(self.settings.per_host_max_concurrent)
                        if self.settings.per_host_max_concurrent > 0
                        else None
                    )
                )
                self._slots[host] = slot
            slot.last_used = time.monotonic()
            return slot

    def _sweep_locked(self) -> None:
        """回收空闲 host 条目。爬虫会碰到无穷多域名，不回收就是内存泄漏。"""
        if len(self._slots) < self.settings.max_tracked_hosts:
            return
        cutoff = time.monotonic() - self.settings.idle_ttl
        stale = [h for h, s in self._slots.items() if s.active == 0 and s.waiting == 0 and s.last_used < cutoff]
        for host in stale:
            del self._slots[host]
        if stale:
            log.debug(f"回收了 {len(stale)} 个空闲 host 限流条目")


def build_async_limiter(downloader_config: dict[str, object] | None) -> AsyncHostLimiter:
    return AsyncHostLimiter(LimiterSettings.from_config(downloader_config))  # type: ignore[arg-type]


__all__ = ["AsyncHostLimiter", "build_async_limiter"]
