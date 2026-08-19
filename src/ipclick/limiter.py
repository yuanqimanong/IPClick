from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
import math
import threading
import time
from typing import Any, final
from urllib.parse import urlsplit

from ipclick.exceptions import ConfigError, IPClickError
from ipclick.utils.log_util import log


class HostLimitTimeout(IPClickError):
    pass


def _as_float(value: Any, field: str, default: float, *, minimum: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ConfigError(f"[DOWNLOADER] {field} 期望数字，得到布尔值 {value!r}")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"[DOWNLOADER] {field} 期望数字，得到 {value!r}") from None
    if result < minimum:
        raise ConfigError(f"[DOWNLOADER] {field} 不能小于 {minimum:g}，得到 {result:g}")
    return result


def _as_int(value: Any, field: str, default: int, *, minimum: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ConfigError(f"[DOWNLOADER] {field} 期望整数，得到布尔值 {value!r}")
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"[DOWNLOADER] {field} 期望整数，得到 {value!r}") from None
    if result < minimum:
        raise ConfigError(f"[DOWNLOADER] {field} 不能小于 {minimum}，得到 {result}")
    return result


@dataclass(frozen=True)
class LimiterSettings:
    per_host_max_concurrent: int = 0
    per_host_qps: float = 0.0
    per_host_burst: int = 0
    wait_timeout: float = 30.0
    idle_ttl: float = 300.0
    max_tracked_hosts: int = 10_000

    @property
    def enabled(self) -> bool:
        return self.per_host_max_concurrent > 0 or self.per_host_qps > 0

    @property
    def burst(self) -> int:
        if self.per_host_burst > 0:
            return self.per_host_burst
        return max(1, math.ceil(self.per_host_qps)) if self.per_host_qps > 0 else 0

    @classmethod
    def from_config(cls, downloader_config: dict[str, Any] | None) -> "LimiterSettings":
        config = dict(downloader_config or {})
        concurrency = dict(config.get("concurrency") or {})
        rate = dict(config.get("rate_limit") or {})
        defaults = cls()
        return cls(
            per_host_max_concurrent=_as_int(
                concurrency.get("per_host_max_concurrent"),
                "concurrency.per_host_max_concurrent",
                defaults.per_host_max_concurrent,
            ),
            per_host_qps=_as_float(rate.get("per_host_qps"), "rate_limit.per_host_qps", defaults.per_host_qps),
            per_host_burst=_as_int(rate.get("per_host_burst"), "rate_limit.per_host_burst", defaults.per_host_burst),
            wait_timeout=_as_float(
                concurrency.get("per_host_wait_timeout"),
                "concurrency.per_host_wait_timeout",
                defaults.wait_timeout,
                minimum=0.0,
            ),
            idle_ttl=_as_float(
                concurrency.get("per_host_idle_ttl"),
                "concurrency.per_host_idle_ttl",
                defaults.idle_ttl,
                minimum=1.0,
            ),
            max_tracked_hosts=_as_int(
                concurrency.get("max_tracked_hosts"),
                "concurrency.max_tracked_hosts",
                defaults.max_tracked_hosts,
                minimum=16,
            ),
        )


def host_of(url: str) -> str:
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
        return self.active == 0 and self.waiting == 0


class HostLimiter:
    def __init__(self, settings: LimiterSettings | None = None):
        self.settings: LimiterSettings = settings or LimiterSettings()
        self._share_qps: float | None = None
        self._slots: dict[str, _HostSlot] = {}
        self._slots_lock: threading.Lock = threading.Lock()
        self._last_sweep: float = time.monotonic()

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

    @contextmanager
    def acquire(self, url: str, timeout: float | None = None) -> Generator[None]:
        settings = self.settings
        host = host_of(url) if settings.enabled else ""
        if not host:
            yield
            return

        slot = self._slot_for(host)
        deadline = time.monotonic() + (settings.wait_timeout if timeout is None else max(0.0, timeout))

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
                slot.active += 1
            return False

        with slot.lock:
            slot.waiting += 1
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
                slot = _HostSlot(self.settings.per_host_max_concurrent, math.ceil(self.effective_burst))
                self._slots[host] = slot
            slot.last_used = time.monotonic()
            return slot

    def _maybe_sweep_locked(self) -> None:
        now = time.monotonic()
        over_limit = len(self._slots) >= self.settings.max_tracked_hosts
        if not over_limit and now - self._last_sweep < self.settings.idle_ttl:
            return

        self._last_sweep = now
        ttl = 0.0 if over_limit else self.settings.idle_ttl
        stale = [h for h, s in self._slots.items() if s.idle and now - s.last_used > ttl]
        for host in stale:
            del self._slots[host]

        if over_limit and len(self._slots) >= self.settings.max_tracked_hosts:
            log.warning(
                f"限流器跟踪的 host 数已达上限 {self.settings.max_tracked_hosts} 且均在使用中，"
                f"请调大 [DOWNLOADER.concurrency].max_tracked_hosts"
            )
        elif stale:
            log.debug(f"限流器回收了 {len(stale)} 个空闲 host 条目，剩余 {len(self._slots)}")

    def snapshot(self) -> dict[str, Any]:
        with self._slots_lock:
            return {
                "enabled": self.settings.enabled,
                "per_host_max_concurrent": self.settings.per_host_max_concurrent,
                "per_host_qps": self.settings.per_host_qps,
                "tracked_hosts": len(self._slots),
                "active": {h: s.active for h, s in self._slots.items() if s.active},
            }


def build_limiter(downloader_config: dict[str, Any] | None) -> HostLimiter:
    config = dict(downloader_config or {})
    rate = dict(config.get("rate_limit") or {})
    backend = str(rate.get("backend") or "memory").strip().lower()
    if backend not in ("", "memory", "local"):
        raise ConfigError(
            f"未知的限流后端 {backend!r}。0.3 起只支持 memory——"
            f"集群限流由入口节点统一计算，不再需要 Redis。请删掉 [DOWNLOADER.rate_limit].backend"
        )
    return HostLimiter(LimiterSettings.from_config(config))


__all__ = ["HostLimitTimeout", "HostLimiter", "LimiterSettings", "build_limiter", "host_of"]


def cluster_share(qps: float, live_nodes: int) -> float:
    if qps <= 0:
        return 0.0
    return qps / max(1, live_nodes)
