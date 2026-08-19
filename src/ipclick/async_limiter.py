"""事件循环友好的逐主机并发与速率限流器。"""

import asyncio
from collections.abc import AsyncGenerator
import contextlib
from dataclasses import dataclass, field
import time

from ipclick.limiter import HostLimitTimeout, LimiterSettings, host_of
from ipclick.utils.log_util import log


@dataclass
class _AsyncSlot:
    semaphore: asyncio.Semaphore | None
    next_available: float = 0.0
    active: int = 0
    waiting: int = 0
    last_used: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class AsyncHostLimiter:
    """使用 asyncio 原语实现的逐主机限流器。"""

    def __init__(self, settings: LimiterSettings | None = None) -> None:
        """创建限流器；所有方法应在同一个事件循环中使用。"""
        self.settings: LimiterSettings = settings or LimiterSettings()
        self._share_qps: float | None = None
        self._slots: dict[str, _AsyncSlot] = {}
        self._slots_lock: asyncio.Lock = asyncio.Lock()

    def set_cluster_size(self, live_nodes: int) -> None:
        """按存活节点数均分配置的全局 QPS。"""
        from ipclick.limiter import cluster_share

        configured = self.settings.per_host_qps
        self._share_qps = cluster_share(configured, live_nodes) if live_nodes > 1 else None
        if self._share_qps is not None:
            log.info(f"集群限流分片：{configured:g} QPS / {live_nodes} 个存活节点 = 本节点 {self._share_qps:g} QPS")

    @property
    def effective_qps(self) -> float:
        """返回本实例实际执行的单主机 QPS。"""
        return self._share_qps if self._share_qps is not None else self.settings.per_host_qps

    @property
    def effective_burst(self) -> float:
        """返回按集群份额缩放后的突发额度。"""
        configured = float(self.settings.burst)
        if self._share_qps is None or self.settings.per_host_qps <= 0:
            return configured
        ratio = self._share_qps / self.settings.per_host_qps
        return max(1.0, configured * ratio)

    def __len__(self) -> int:
        return len(self._slots)

    @contextlib.asynccontextmanager
    async def acquire(self, url: str, timeout: float | None = None) -> AsyncGenerator[None]:
        """在截止时间前异步获取并发和速率额度。"""
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

        # 与同步实现一致：等待速率令牌时仍计入在途并发。
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
        qps = self.effective_qps
        if qps <= 0:
            return

        interval = 1.0 / qps
        # 首个请求本身占一个突发名额，因此只允许额外回退 burst - 1 个间隔。
        max_lag = max(0.0, self.effective_burst - 1.0) * interval

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
                if len(self._slots) >= self.settings.max_tracked_hosts:
                    raise HostLimitTimeout(
                        f"限流器跟踪的 host 数已达上限 {self.settings.max_tracked_hosts} 且均在使用中；"
                        "请调大 [DOWNLOADER.concurrency].max_tracked_hosts"
                    )
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
        if len(self._slots) < self.settings.max_tracked_hosts:
            return
        cutoff = time.monotonic() - self.settings.idle_ttl
        idle = [(slot.last_used, host) for host, slot in self._slots.items() if slot.active == 0 and slot.waiting == 0]
        stale = [host for last_used, host in idle if last_used < cutoff]
        for host in stale:
            del self._slots[host]

        # 容量压力下不等待 TTL，直接驱逐最久未使用的空闲 host。
        remaining_idle = [(last_used, host) for last_used, host in idle if host in self._slots]
        if len(self._slots) >= self.settings.max_tracked_hosts and remaining_idle:
            _, lru_host = min(remaining_idle)
            del self._slots[lru_host]
            stale.append(lru_host)
        if stale:
            log.debug(f"回收了 {len(stale)} 个空闲 host 限流条目")


def build_async_limiter(downloader_config: dict[str, object] | None) -> AsyncHostLimiter:
    """从下载器配置构建异步内存限流器。"""
    return AsyncHostLimiter(LimiterSettings.from_config(downloader_config))


__all__ = ["AsyncHostLimiter", "build_async_limiter"]
