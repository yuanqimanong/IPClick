"""按目标主机实施并发上限与令牌桶速率限制。"""

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
import math
import threading
import time
from typing import Any, final
from urllib.parse import urlsplit

from ipclick.exceptions import ConfigError, IPClickError
from ipclick.utils.coerce import require_float, require_int
from ipclick.utils.config_util import section
from ipclick.utils.log_util import log


_SECTION = "[DOWNLOADER]"


class HostLimitTimeout(IPClickError):
    """等待主机并发或速率额度超过截止时间。"""


@dataclass(frozen=True)
class LimiterSettings:
    """单主机限流配置；数值为零时禁用对应限制。"""

    per_host_max_concurrent: int = 0
    per_host_qps: float = 0.0
    per_host_burst: int = 0
    wait_timeout: float = 30.0
    idle_ttl: float = 300.0
    max_tracked_hosts: int = 10_000

    @property
    def enabled(self) -> bool:
        """返回是否至少启用了一种限制。"""
        return self.per_host_max_concurrent > 0 or self.per_host_qps > 0

    @property
    def burst(self) -> int:
        """返回显式突发量，或根据 QPS 推导的默认值。"""
        if self.per_host_burst > 0:
            return self.per_host_burst
        return max(1, math.ceil(self.per_host_qps)) if self.per_host_qps > 0 else 0

    @classmethod
    def from_config(cls, downloader_config: dict[str, Any] | None) -> "LimiterSettings":
        """从 ``[DOWNLOADER]`` 配置解析并校验限流参数。"""
        config = dict(downloader_config or {})
        concurrency = section(config, "concurrency")
        rate = section(config, "rate_limit")
        defaults = cls()
        return cls(
            per_host_max_concurrent=require_int(
                concurrency.get("per_host_max_concurrent"),
                f"{_SECTION} concurrency.per_host_max_concurrent",
                defaults.per_host_max_concurrent,
            ),
            per_host_qps=require_float(
                rate.get("per_host_qps"), f"{_SECTION} rate_limit.per_host_qps", defaults.per_host_qps
            ),
            per_host_burst=require_int(
                rate.get("per_host_burst"), f"{_SECTION} rate_limit.per_host_burst", defaults.per_host_burst
            ),
            wait_timeout=require_float(
                concurrency.get("per_host_wait_timeout"),
                f"{_SECTION} concurrency.per_host_wait_timeout",
                defaults.wait_timeout,
            ),
            idle_ttl=require_float(
                concurrency.get("per_host_idle_ttl"),
                f"{_SECTION} concurrency.per_host_idle_ttl",
                defaults.idle_ttl,
                minimum=1.0,
            ),
            max_tracked_hosts=require_int(
                concurrency.get("max_tracked_hosts"),
                f"{_SECTION} concurrency.max_tracked_hosts",
                defaults.max_tracked_hosts,
                minimum=16,
            ),
        )


def host_of(url: str) -> str:
    """提取小写 hostname；无效或无主机名的 URL 返回空串。"""
    try:
        hostname = urlsplit(url).hostname
    except ValueError:
        return ""
    return (hostname or "").lower()


@final
class _HostSlot:
    __slots__ = ("active", "last_used", "lock", "semaphore", "tokens", "updated_at", "waiting")

    def __init__(self, max_concurrent: int, burst: int):
        self.semaphore: threading.Semaphore | None = threading.Semaphore(max_concurrent) if max_concurrent else None
        self.lock: threading.Lock = threading.Lock()
        self.tokens: float = float(burst)
        self.updated_at: float = time.monotonic()
        self.active: int = 0
        self.waiting: int = 0
        self.last_used: float = time.monotonic()

    @property
    def idle(self) -> bool:
        """返回该 host 是否没有活动或等待中的请求。"""
        return self.active == 0 and self.waiting == 0


class HostLimiter:
    """线程安全的逐主机并发信号量与令牌桶限流器。"""

    def __init__(self, settings: LimiterSettings | None = None):
        """创建限流器，host 状态按需分配。"""
        self.settings: LimiterSettings = settings or LimiterSettings()
        self._share_qps: float | None = None
        self._slots: dict[str, _HostSlot] = {}
        self._slots_lock: threading.Lock = threading.Lock()
        self._last_sweep: float = time.monotonic()

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

    @contextmanager
    def acquire(self, url: str, timeout: float | None = None) -> Generator[None]:
        """在截止时间前依次获取并发和速率额度。"""
        settings = self.settings
        host = host_of(url) if settings.enabled else ""
        if not host:
            yield
            return

        slot = self._slot_for(host)
        deadline = time.monotonic() + (settings.wait_timeout if timeout is None else max(0.0, timeout))

        # 先占并发槽，再取速率令牌，确保等待速率期间也受总并发保护。
        acquired = self._acquire_concurrency(slot, host, deadline)
        try:
            self._acquire_token(slot, host, deadline)
            yield
        finally:
            with slot.lock:
                slot.active -= 1
                slot.last_used = time.monotonic()
            if acquired and slot.semaphore is not None:
                slot.semaphore.release()

    def _acquire_concurrency(self, slot: _HostSlot, host: str, deadline: float) -> bool:
        if slot.semaphore is None:
            with slot.lock:
                slot.waiting -= 1
                slot.active += 1
            return False

        try:
            remaining = deadline - time.monotonic()
            got = slot.semaphore.acquire(timeout=remaining) if remaining > 0 else slot.semaphore.acquire(blocking=False)
        finally:
            with slot.lock:
                slot.waiting -= 1

        if not got:
            raise HostLimitTimeout(
                f"等待 {host} 的并发额度超时（上限 {self.settings.per_host_max_concurrent} 个并发，"
                f"已等待 {self.settings.wait_timeout:.1f} 秒）"
            )

        with slot.lock:
            slot.active += 1
        return True

    def _acquire_token(self, slot: _HostSlot, host: str, deadline: float) -> None:
        qps = self.effective_qps
        if qps <= 0:
            return

        burst = self.effective_burst
        while True:
            with slot.lock:
                now = time.monotonic()
                slot.tokens = min(burst, slot.tokens + (now - slot.updated_at) * qps)
                slot.updated_at = now
                if slot.tokens >= 1.0:
                    slot.tokens -= 1.0
                    return
                wait = (1.0 - slot.tokens) / qps

            if now + wait > deadline:
                raise HostLimitTimeout(
                    f"等待 {host} 的速率额度超时（上限 {qps:g} QPS，已等待 {self.settings.wait_timeout:.1f} 秒）"
                )
            time.sleep(wait)

    def _slot_for(self, host: str) -> _HostSlot:
        with self._slots_lock:
            slot = self._slots.get(host)
            if slot is None:
                self._maybe_sweep_locked()
                if len(self._slots) >= self.settings.max_tracked_hosts:
                    raise HostLimitTimeout(
                        f"限流器跟踪的 host 数已达上限 {self.settings.max_tracked_hosts} 且均在使用中；"
                        "请调大 [DOWNLOADER.concurrency].max_tracked_hosts"
                    )
                slot = _HostSlot(self.settings.per_host_max_concurrent, math.ceil(self.effective_burst))
                self._slots[host] = slot
            with slot.lock:
                # 在交还 slot 前先登记等待者，防止并发 sweep 删除刚取出的空闲条目。
                slot.waiting += 1
                slot.last_used = time.monotonic()
            return slot

    def _maybe_sweep_locked(self) -> None:
        now = time.monotonic()
        over_limit = len(self._slots) >= self.settings.max_tracked_hosts
        if not over_limit and now - self._last_sweep < self.settings.idle_ttl:
            return

        self._last_sweep = now
        ttl = self.settings.idle_ttl
        idle: list[tuple[float, str]] = []
        for host, slot in self._slots.items():
            # 固定锁顺序为 slots -> slot，避免 snapshot/acquire 与回收互相死锁。
            with slot.lock:
                if slot.idle:
                    idle.append((slot.last_used, host))

        stale = [host for last_used, host in idle if now - last_used > ttl]
        for host in stale:
            del self._slots[host]

        # TTL 回收后仍满时，额外驱逐最久未使用的空闲项；活动项绝不驱逐。
        remaining_idle = [(last_used, host) for last_used, host in idle if host in self._slots]
        if len(self._slots) >= self.settings.max_tracked_hosts and remaining_idle:
            _, lru_host = min(remaining_idle)
            del self._slots[lru_host]
            stale.append(lru_host)

        if stale:
            log.debug(f"限流器回收了 {len(stale)} 个空闲 host 条目，剩余 {len(self._slots)}")

    def snapshot(self) -> dict[str, Any]:
        """返回用于诊断的无副作用状态快照。"""
        with self._slots_lock:
            active: dict[str, int] = {}
            for host, slot in self._slots.items():
                with slot.lock:
                    if slot.active:
                        active[host] = slot.active
            return {
                "enabled": self.settings.enabled,
                "per_host_max_concurrent": self.settings.per_host_max_concurrent,
                "per_host_qps": self.settings.per_host_qps,
                "tracked_hosts": len(self._slots),
                "active": active,
            }


def build_limiter(downloader_config: dict[str, Any] | None) -> HostLimiter:
    """校验后端配置并构建内存限流器。"""
    config = dict(downloader_config or {})
    rate = section(config, "rate_limit")
    backend = str(rate.get("backend") or "memory").strip().lower()
    if backend not in ("", "memory", "local"):
        raise ConfigError(
            f"未知的限流后端 {backend!r}。0.3 起只支持 memory——"
            f"集群限流由入口节点统一计算，不再需要 Redis。请删掉 [DOWNLOADER.rate_limit].backend"
        )
    return HostLimiter(LimiterSettings.from_config(config))


__all__ = ["HostLimitTimeout", "HostLimiter", "LimiterSettings", "build_limiter", "host_of"]


def cluster_share(qps: float, live_nodes: int) -> float:
    """计算每个存活集群节点应承担的 QPS 份额。"""
    if qps <= 0:
        return 0.0
    return qps / max(1, live_nodes)
