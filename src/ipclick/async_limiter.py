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
    def __init__(self, settings: LimiterSettings | None = None) -> None:
        self.settings: LimiterSettings = settings or LimiterSettings()
        self._share_qps: float | None = None
        self._slots: dict[str, _AsyncSlot] = {}
        self._slots_lock: asyncio.Lock = asyncio.Lock()

    def set_cluster_size(self, live_nodes: int) -> None:
        from ipclick.limiter import cluster_share

        configured = self.settings.per_host_qps
        self._share_qps = cluster_share(configured, live_nodes) if live_nodes > 1 else None
        if self._share_qps is not None:
            log.info(f"集群限流分片：{configured:g} QPS / {live_nodes} 个存活节点 = 本节点 {self._share_qps:g} QPS")

    @property
    def effective_qps(self) -> float:
        return self._share_qps if self._share_qps is not None else self.settings.per_host_qps

    @property
    def effective_burst(self) -> float:
        configured = float(self.settings.burst)
        if self._share_qps is None or self.settings.per_host_qps <= 0:
            return configured
        ratio = self._share_qps / self.settings.per_host_qps
        return max(1.0, configured * ratio)

    def __len__(self) -> int:
        return len(self._slots)

    @contextlib.asynccontextmanager
    async def acquire(self, url: str, timeout: float | None = None) -> AsyncGenerator[None]:
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
        qps = self.effective_qps
        if qps <= 0:
            return

        interval = 1.0 / qps
        max_lag = max(0.0, self.effective_burst) * interval

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
        if len(self._slots) < self.settings.max_tracked_hosts:
            return
        cutoff = time.monotonic() - self.settings.idle_ttl
        stale = [h for h, s in self._slots.items() if s.active == 0 and s.waiting == 0 and s.last_used < cutoff]
        for host in stale:
            del self._slots[host]
        if stale:
            log.debug(f"回收了 {len(stale)} 个空闲 host 限流条目")


def build_async_limiter(downloader_config: dict[str, object] | None) -> AsyncHostLimiter:
    return AsyncHostLimiter(LimiterSettings.from_config(downloader_config))


__all__ = ["AsyncHostLimiter", "build_async_limiter"]
