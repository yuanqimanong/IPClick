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


def _as_float(value: Any, field: str, default: float, *, minimum: float = 0.0) -> float:
    """读一个浮点配置项。**写错就报错，不静默取默认值。**

    这两个解析器管的是限流，而限流是**保护性开关**——它的作用是别把目标站点
    打到封你。原先的实现是无声回落：``per_host_qps = "abc"`` 和 ``-5`` 都会
    变成 0.0，而 0.0 在这里的语义恰好是"不限速"。

    也就是说：配置写错的后果是限流被彻底关掉，且日志里一个字都没有。你以为
    自己配了 100 QPS，实际在满速锤对方，直到对面开始返 429 或直接封 IP 才发现。
    对一个保护性开关来说，这是最坏的失败方向——**它必须 fail-closed 或者吵闹地
    失败，不能安静地打开闸门**。

    键不存在（``None``）仍然走默认值：那是"没配置"，不是"配错了"。
    """
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
    """读一个整数配置项。语义同 :func:`_as_float`——写错就报错。"""
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
    """来自 ``[DOWNLOADER.concurrency]`` 与 ``[DOWNLOADER.rate_limit]``。"""

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
        self.semaphore: threading.Semaphore | None = threading.Semaphore(max_concurrent) if max_concurrent else None
        self.lock: threading.Lock = threading.Lock()
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
        self._share_qps: float | None = None
        self._slots: dict[str, _HostSlot] = {}
        self._slots_lock: threading.Lock = threading.Lock()
        self._last_sweep: float = time.monotonic()

    def set_cluster_size(self, live_nodes: int) -> None:
        """告知当前存活节点数，据此重算本节点的 QPS 份额。

        只有客户端分发（``forward = "off"``）才该调这个。传 1 或不调 = 不分片。
        节点上下线时重复调用即可，份额立刻生效（已在等待的请求按旧份额走完，
        不回溯——回溯会让那些请求被推迟到一个它们本来不该等的时刻）。
        """
        from ipclick.limiter import cluster_share

        configured = self.settings.per_host_qps
        self._share_qps = cluster_share(configured, live_nodes) if live_nodes > 1 else None
        if self._share_qps is not None:
            log.info(f"集群限流分片：{configured:g} QPS / {live_nodes} 个存活节点 = 本节点 {self._share_qps:g} QPS")

    @property
    def effective_qps(self) -> float:
        """本节点实际生效的 QPS。未分片时就是配置值。"""
        return self._share_qps if self._share_qps is not None else self.settings.per_host_qps

    @property
    def effective_burst(self) -> float:
        """本节点实际生效的突发额度（令牌桶容量）。

        **必须和 :attr:`effective_qps` 一起分片。** 只切稳态速率、不切桶容量的话：
        配 100 QPS 部署 4 台，每台稳态 25 QPS 是对的，但每台仍攒 100 个令牌，
        集群瞬时能放出 400 个——而 burst 的全部意义就是"允许多大的瞬时尖峰"。
        10 台就是 1000。

        这种漏法特别难自查：**稳态是对的**，压测跑一分钟取平均完全正常，
        只在**流量刚起来的那一下**暴露。而那恰恰是目标站点风控最容易触发的时刻，
        于是现象变成"平时好好的，一重启/一扩容就被封"。

        向下取整会把小集群的 burst 抹成 0（100 QPS / 128 节点），所以兜底到 1：
        令牌桶容量为 0 意味着永远拿不到令牌，那是挂死不是限流。
        """
        configured = float(self.settings.burst)
        if self._share_qps is None or self.settings.per_host_qps <= 0:
            return configured
        ratio = self._share_qps / self.settings.per_host_qps
        return max(1.0, configured * ratio)

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
        """拿并发槽。返回是否真的占用了信号量（未限并发时为 False）。"""
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
        """令牌桶。没有令牌就睡到下一个令牌生成，但不超过 deadline。"""
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


def cluster_share(qps: float, live_nodes: int) -> float:
    """把全局 QPS 预算切给本节点。

    **只在客户端分发（``[CLUSTER].forward = "off"``）下才需要这一步。**
    那种形态里调用方直连每一台节点，每台各算各的限额，加起来就是
    ``N × per_host_qps`` —— 配了 100 QPS、部署了四台，目标站点实际挨 400。
    这是"配了限流还是被封"的一类典型原因，而且从任何单台机器的视角看都正常。

    服务端转发（``forward = "on"``）不走这里：所有任务都经入口节点，在那一台上
    算就是全局的，本来就精确。

    切法是**按存活节点数均分**，份额随健康探测自动变化：一台挂了，幸存者下一轮
    就各自分到更多，不需要任何协调。代价说清楚——负载不均时会浪费额度：四台
    机器三台闲着，忙的那台仍然只能用 1/4。想要"精确且不浪费"得引入协调者
    （选主 + 租约），那是另一套复杂度，也带来协调者失联这个新故障模式。
    真需要精确又不想浪费，切到 ``forward = "on"`` 是更划算的路。

    Args:
        qps: 配置里的 ``per_host_qps``（0 表示不限速）。
        live_nodes: 当前存活的节点数。0 或 1 都按"就我自己"处理。
    """
    if qps <= 0:
        return 0.0
    return qps / max(1, live_nodes)
