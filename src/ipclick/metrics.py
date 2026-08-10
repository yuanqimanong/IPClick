"""Prometheus 指标。

``prometheus_client`` 是**可选依赖**（``pip install "ipclick[metrics]"``）。
未安装时本模块的所有埋点降级为无操作，调用方不需要写任何 if——
把"有没有装"这件事收敛在这一个文件里，业务代码保持干净。

标签设计上有一条硬规则：**绝不用目标 URL 或目标主机名做标签**。
理由有两条，任何一条都足以否决：

1. 基数爆炸。爬虫场景下目标 URL 是无界的，每个不同的 URL 都会在 Prometheus
   里生成一条独立时间序列，很快把它撑爆。
2. 信息泄漏。指标端点通常比业务端口更少设防，把抓取目标暴露在那里等于公开
   业务意图。

因此所有标签都取自有限集合：适配器名、HTTP 方法、结果分类。
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
import threading
from typing import Any

from ipclick.utils.log_util import log


# 可选依赖：标成 Any，让"模块或 None"这种运行时形态不必到处写 ignore
_prom: Any

try:
    import prometheus_client as _prom
except ImportError:  # pragma: no cover - 取决于是否装了可选依赖
    _prom = None

METRICS_AVAILABLE: bool = _prom is not None


#: 请求耗时分桶（秒）。覆盖从本地缓存命中到接近默认超时（300s）的范围。
_DURATION_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0)

#: 响应体大小分桶（字节），1KB 到 100MB。
_SIZE_BUCKETS = (1024, 10240, 102400, 1048576, 10485760, 104857600)


def classify_status(status_code: int) -> str:
    """把状态码归成有限的几类，避免用原始状态码做标签导致基数过大。"""
    if status_code < 0:
        return "failure"  # 连接层失败，没拿到 HTTP 响应
    if status_code < 300:
        return "2xx"
    if status_code < 400:
        return "3xx"
    if status_code < 500:
        return "4xx"
    return "5xx"


class _NoopMetric:
    """未安装 prometheus_client 时的占位符，吞掉所有调用。"""

    def labels(self, *_args: Any, **_kwargs: Any) -> _NoopMetric:
        return self

    def inc(self, *_args: Any, **_kwargs: Any) -> None: ...

    def dec(self, *_args: Any, **_kwargs: Any) -> None: ...

    def observe(self, *_args: Any, **_kwargs: Any) -> None: ...

    def set(self, *_args: Any, **_kwargs: Any) -> None: ...

    def info(self, *_args: Any, **_kwargs: Any) -> None: ...


class Metrics:
    """IPClick 的指标集合。

    未安装 prometheus_client 时所有字段都是 :class:`_NoopMetric`，
    埋点调用照常写、不产生任何开销。
    """

    def __init__(self, registry: Any = None, version: str = ""):
        # 统一从 _prom 判断，不要用 METRICS_AVAILABLE——两处判断源不同的话，
        # 只要有人改了其中一个就会出现"enabled=True 但指标全是 noop"这种错位。
        self.enabled: bool = _prom is not None
        self._server_started: bool = False
        self._lock: threading.Lock = threading.Lock()

        # 这些字段要么是 prometheus 的指标对象，要么是 _NoopMetric，
        # 两者接口一致但没有共同基类，所以统一标成 Any。
        self.requests_total: Any
        self.request_duration: Any
        self.response_size: Any
        self.in_flight: Any
        self.retries_total: Any
        self.rejected_total: Any
        self.build_info: Any
        self.registry: Any

        if _prom is None:
            noop = _NoopMetric()
            self.requests_total = noop
            self.request_duration = noop
            self.response_size = noop
            self.in_flight = noop
            self.retries_total = noop
            self.rejected_total = noop
            self.build_info = noop
            self.registry = None
            return

        self.registry = registry if registry is not None else _prom.CollectorRegistry(auto_describe=True)

        self.requests_total = _prom.Counter(
            "ipclick_requests_total",
            "处理过的下载请求总数",
            ["adapter", "method", "outcome"],
            registry=self.registry,
        )
        self.request_duration = _prom.Histogram(
            "ipclick_request_duration_seconds",
            "下载请求耗时（服务端视角，含重试）",
            ["adapter"],
            buckets=_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.response_size = _prom.Histogram(
            "ipclick_response_bytes",
            "响应体大小",
            ["adapter"],
            buckets=_SIZE_BUCKETS,
            registry=self.registry,
        )
        self.in_flight = _prom.Gauge(
            "ipclick_requests_in_flight",
            "正在处理的请求数（可用于判断 worker 线程池是否吃紧）",
            registry=self.registry,
        )
        self.retries_total = _prom.Counter(
            "ipclick_retries_total",
            "适配器发起的重试次数",
            ["adapter", "reason"],
            registry=self.registry,
        )
        self.rejected_total = _prom.Counter(
            "ipclick_rejected_total",
            "在执行下载前被拒绝的请求数",
            ["reason"],
            registry=self.registry,
        )
        self.build_info = _prom.Info("ipclick_build", "构建信息", registry=self.registry)
        if version:
            self.build_info.info({"version": version})

    def start_http_server(self, port: int, host: str = "0.0.0.0") -> bool:
        """在独立端口上暴露 ``/metrics``。

        指标走单独的 HTTP 端口而不是复用 gRPC 端口，这是 Prometheus 生态的惯例。

        Returns:
            是否成功启动。
        """
        if _prom is None:
            log.warning('未安装 prometheus_client，指标端点不会启动。安装：pip install "ipclick[metrics]"')
            return False

        with self._lock:
            if self._server_started:
                return True
            try:
                _prom.start_http_server(port, addr=host, registry=self.registry)
            except OSError as e:
                log.error(f"指标端点启动失败 {host}:{port}: {e}")
                return False
            self._server_started = True

        log.info(f"Prometheus 指标端点: http://{host}:{port}/metrics")
        return True

    # ------------------------------------------------------------------ #
    # 埋点辅助
    # ------------------------------------------------------------------ #

    @contextmanager
    def track_request(self, adapter: str, method: str) -> Generator[dict[str, Any]]:
        """包住一次请求处理：自动记在途数、耗时，退出时按结果记数。

        用法::

            with metrics.track_request("curl_cffi", "GET") as ctx:
                response = do_work()
                ctx["status_code"] = response.status_code
                ctx["size"] = len(response.content or b"")
        """
        import time

        ctx: dict[str, Any] = {"status_code": -1, "size": 0}
        self.in_flight.inc()
        start = time.monotonic()
        try:
            yield ctx
        finally:
            elapsed = time.monotonic() - start
            self.in_flight.dec()
            self.request_duration.labels(adapter=adapter).observe(elapsed)

            outcome = classify_status(int(ctx.get("status_code", -1)))
            self.requests_total.labels(adapter=adapter, method=method, outcome=outcome).inc()

            size = int(ctx.get("size", 0) or 0)
            if size > 0:
                self.response_size.labels(adapter=adapter).observe(size)

    def record_retry(self, adapter: str, reason: str) -> None:
        self.retries_total.labels(adapter=adapter, reason=reason).inc()

    def record_rejected(self, reason: str) -> None:
        """记录在执行下载前就被拒绝的请求（鉴权失败、SSRF 拦截、参数非法等）。"""
        self.rejected_total.labels(reason=reason).inc()


def _package_version() -> str:
    """直接查包元数据，不 import ipclick——那会形成
    ipclick.__init__ -> sdk -> ... -> metrics -> ipclick 的循环导入。
    """
    import importlib.metadata

    try:
        return importlib.metadata.version("ipclick")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - 仅在未安装时
        return "unknown"


#: 进程级单例。埋点处直接用它，不必层层传递。
_metrics: Metrics | None = None
_metrics_lock = threading.Lock()


def get_metrics() -> Metrics:
    """取进程级指标单例（首次调用时创建）。"""
    global _metrics
    if _metrics is not None:
        return _metrics
    with _metrics_lock:
        if _metrics is None:
            _metrics = Metrics(version=_package_version())
    return _metrics


def reset_metrics() -> None:
    """重置单例。仅供测试隔离使用。"""
    global _metrics
    with _metrics_lock:
        _metrics = None


__all__ = [
    "METRICS_AVAILABLE",
    "Metrics",
    "classify_status",
    "get_metrics",
    "reset_metrics",
]
