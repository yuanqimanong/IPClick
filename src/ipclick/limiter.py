"""按目标 host 的并发与速率限制。

服务端会代替调用方向外发请求。没有这一层的话，一个批量任务就能对同一个站点
瞬间打出几十上百个并发连接——对方大概率先限速再封 IP，本机的出口带宽也会被
一个 host 吃干净。

两道闸门，都是**按 host 独立**计数：

* **并发**（信号量）：同一时刻最多几个在途请求。这是硬保证。
* **速率**（令牌桶）：每秒最多发起几个请求，允许一定突发。

取额度的顺序是**先并发槽、后令牌**。并发槽是要保证的那个上限，先拿住；令牌
紧挨着真正的 HTTP 请求再取，这样"每秒 N 个"限的就是真实发出去的请求，而不是
"进入排队的请求"。

⚠️ 线程占用
-----------
gRPC 服务端是一请求一线程。在这里等额度会**占着那个 worker 线程**——把
``per_host_max_concurrent`` 设得很小、同时又有大量请求打向同一个 host 时，
线程池会被排队的请求占满，其他 host 的请求也跟着饿死。因此：

* 等待有硬性上限（``wait_timeout``），超时就失败，不会无限期占用线程；
* ``[SERVER].max_workers`` 要留出足够余量。

真要做到"排队不占线程"，得把服务端改成异步的（``grpc.aio``），那是另一件事。
"""

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
    """在超时时间内没能拿到某个 host 的额度。

    刻意不继承 TransportError：这不是网络问题，是本机的限流策略生效了。
    混进传输失败会让人去查目标站点，而实际该调的是配置。
    """


def _as_float(value: Any, default: float, *, minimum: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result >= minimum else default


def _as_int(value: Any, default: int, *, minimum: int = 0) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result if result >= minimum else default


@dataclass(frozen=True)
class LimiterSettings:
    """来自 ``[DOWNLOADER.concurrency]`` 与 ``[DOWNLOADER.rate_limit]``。"""

    #: 单个 host 的并发上限。0 = 不限制
    per_host_max_concurrent: int = 0
    #: 单个 host 每秒最多发起几个请求。0 = 不限制
    per_host_qps: float = 0.0
    #: 令牌桶容量（突发额度）。0 = 取 ceil(per_host_qps)，即最多攒够一秒的量
    per_host_burst: int = 0
    #: 等待额度的上限（秒）。超时抛 HostLimitTimeout，而不是一直占着 worker 线程
    wait_timeout: float = 30.0
    #: host 条目空闲多久后回收（秒）。爬虫会碰到无穷多域名，不回收就是内存泄漏
    idle_ttl: float = 300.0
    #: 同时跟踪的 host 上限。超过就强制清一次空闲条目
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
                concurrency.get("per_host_max_concurrent"), defaults.per_host_max_concurrent
            ),
            per_host_qps=_as_float(rate.get("per_host_qps"), defaults.per_host_qps),
            per_host_burst=_as_int(rate.get("per_host_burst"), defaults.per_host_burst),
            wait_timeout=_as_float(concurrency.get("per_host_wait_timeout"), defaults.wait_timeout, minimum=0.0),
            idle_ttl=_as_float(concurrency.get("per_host_idle_ttl"), defaults.idle_ttl, minimum=1.0),
            max_tracked_hosts=_as_int(concurrency.get("max_tracked_hosts"), defaults.max_tracked_hosts, minimum=16),
        )


def host_of(url: str) -> str:
    """取用于限流的 host 键。

    只用主机名、不含端口：``example.com:8080`` 和 ``example.com:443`` 通常是
    同一台机器，分开计数就限不住了。大小写归一化，否则 ``Example.com`` 会
    另开一份额度。
    """
    try:
        hostname = urlsplit(url).hostname
    except ValueError:
        return ""
    return (hostname or "").lower()


@final
class _HostSlot:
    """单个 host 的额度状态。"""

    __slots__ = ("active", "last_used", "lock", "semaphore", "tokens", "updated_at", "waiting")

    def __init__(self, max_concurrent: int, burst: int):
        # max_concurrent == 0 表示不限并发，此时不建信号量
        self.semaphore: threading.Semaphore | None = threading.Semaphore(max_concurrent) if max_concurrent else None
        self.lock: threading.Lock = threading.Lock()
        # 令牌桶初始装满，否则服务刚起来的第一批请求会被无谓地拖慢
        self.tokens: float = float(burst)
        self.updated_at: float = time.monotonic()
        self.active: int = 0
        self.waiting: int = 0
        self.last_used: float = time.monotonic()

    @property
    def idle(self) -> bool:
        """没有在途请求、也没有人在等——可以安全回收。"""
        return self.active == 0 and self.waiting == 0


class HostLimiter:
    """按 host 限制并发与速率。线程安全，可重复使用。

    未启用（两项都为 0）时 :meth:`acquire` 是零开销的空操作。
    """

    def __init__(self, settings: LimiterSettings | None = None):
        self.settings: LimiterSettings = settings or LimiterSettings()
        self._slots: dict[str, _HostSlot] = {}
        self._slots_lock: threading.Lock = threading.Lock()
        self._last_sweep: float = time.monotonic()

    # ------------------------------------------------------------------ #
    # 额度
    # ------------------------------------------------------------------ #

    @contextmanager
    def acquire(self, url: str, timeout: float | None = None) -> Generator[None]:
        """取得该 URL 所属 host 的额度，退出上下文时归还。

        Args:
            url: 目标 URL；只取其中的主机名。
            timeout: 等待上限（秒）。None 表示用配置里的 ``wait_timeout``。

        Raises:
            HostLimitTimeout: 超时仍未拿到额度。
        """
        settings = self.settings
        host = host_of(url) if settings.enabled else ""
        if not host:
            # 未启用限流，或 URL 里根本没有主机名（后者会在别处被拦下）
            yield
            return

        slot = self._slot_for(host)
        deadline = time.monotonic() + (settings.wait_timeout if timeout is None else max(0.0, timeout))

        acquired = self._acquire_concurrency(slot, host, deadline)
        try:
            # 令牌紧挨着真正的请求再取：先拿并发槽能保证上限不被突破，
            # 后取令牌能让"每秒 N 个"限的是真实发出去的请求。
            self._acquire_token(slot, host, deadline)
            yield
        finally:
            with slot.lock:
                slot.active -= 1
                slot.last_used = time.monotonic()
            if acquired and slot.semaphore is not None:
                slot.semaphore.release()

    def _acquire_concurrency(self, slot: _HostSlot, host: str, deadline: float) -> bool:
        """拿并发槽。返回是否真的占用了信号量（未限并发时为 False）。"""
        if slot.semaphore is None:
            with slot.lock:
                slot.active += 1
            return False

        with slot.lock:
            slot.waiting += 1
        try:
            remaining = deadline - time.monotonic()
            # timeout<=0 时 Semaphore.acquire 会当成"阻塞等待"，必须显式走非阻塞
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
        """令牌桶。没有令牌就睡到下一个令牌生成，但不超过 deadline。"""
        qps = self.settings.per_host_qps
        if qps <= 0:
            return

        burst = float(self.settings.burst)
        while True:
            with slot.lock:
                now = time.monotonic()
                slot.tokens = min(burst, slot.tokens + (now - slot.updated_at) * qps)
                slot.updated_at = now
                if slot.tokens >= 1.0:
                    slot.tokens -= 1.0
                    return
                # 还差多少令牌，就得等多久
                wait = (1.0 - slot.tokens) / qps

            if now + wait > deadline:
                raise HostLimitTimeout(
                    f"等待 {host} 的速率额度超时（上限 {qps:g} QPS，已等待 {self.settings.wait_timeout:.1f} 秒）"
                )
            time.sleep(wait)

    # ------------------------------------------------------------------ #
    # 条目管理
    # ------------------------------------------------------------------ #

    def _slot_for(self, host: str) -> _HostSlot:
        with self._slots_lock:
            slot = self._slots.get(host)
            if slot is None:
                self._maybe_sweep_locked()
                slot = _HostSlot(self.settings.per_host_max_concurrent, self.settings.burst)
                self._slots[host] = slot
            slot.last_used = time.monotonic()
            return slot

    def _maybe_sweep_locked(self) -> None:
        """回收空闲 host 条目。调用方必须已持有 _slots_lock。

        爬虫会碰到无穷多域名，不回收就是一条稳定的内存泄漏。
        """
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
            # 全都在用，说明配置和实际负载不匹配——只提醒，不强行踢掉在途请求
            log.warning(
                f"限流器跟踪的 host 数已达上限 {self.settings.max_tracked_hosts} 且均在使用中，"
                f"请调大 [DOWNLOADER.concurrency].max_tracked_hosts"
            )
        elif stale:
            log.debug(f"限流器回收了 {len(stale)} 个空闲 host 条目，剩余 {len(self._slots)}")

    def snapshot(self) -> dict[str, Any]:
        """当前状态，供状态页与测试使用。"""
        with self._slots_lock:
            return {
                "enabled": self.settings.enabled,
                "per_host_max_concurrent": self.settings.per_host_max_concurrent,
                "per_host_qps": self.settings.per_host_qps,
                "tracked_hosts": len(self._slots),
                "active": {h: s.active for h, s in self._slots.items() if s.active},
            }


def build_limiter(downloader_config: dict[str, Any] | None) -> HostLimiter:
    """按 ``[DOWNLOADER]`` 配置造出限流器。

    这里曾经支持 ``backend = redis``（用 Lua 脚本做跨节点共享额度）。集群改成
    "主节点转发"之后不再需要中间件：所有任务都从入口节点进来，额度在那一台上
    算就是全局的，多引入一个 Redis 只是多一个会挂的部件。

    显式写了 ``backend`` 且不是内存后端时直接报错——静默降级的话，以为开了
    共享限额、实际每个节点各算各的，问题要到把对方站点打挂才会暴露。
    """
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
