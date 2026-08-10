"""Prometheus 指标。

重点验证两件事：标签基数受控（绝不含 URL），以及未安装可选依赖时能优雅降级。
"""

from collections.abc import Iterator
import socket

import pytest

from ipclick.metrics import METRICS_AVAILABLE, Metrics, classify_status, get_metrics, reset_metrics


pytestmark = pytest.mark.skipif(not METRICS_AVAILABLE, reason="未安装 prometheus_client")


@pytest.fixture
def metrics() -> Iterator[Metrics]:
    """每个用例一套全新的 registry，避免相互干扰。"""
    from prometheus_client import CollectorRegistry

    yield Metrics(registry=CollectorRegistry(), version="test")


def _sample(metrics: Metrics, name: str, **labels: str) -> float:
    """从 registry 里取一个样本值，取不到返回 0。"""
    assert metrics.registry is not None
    for metric in metrics.registry.collect():
        for sample in metric.samples:
            if sample.name == name and all(sample.labels.get(k) == v for k, v in labels.items()):
                return sample.value
    return 0.0


class TestClassifyStatus:
    @pytest.mark.parametrize(
        ("code", "expected"),
        [(-1, "failure"), (200, "2xx"), (204, "2xx"), (301, "3xx"), (404, "4xx"), (429, "4xx"), (500, "5xx")],
    )
    def test_buckets(self, code: int, expected: str):
        assert classify_status(code) == expected

    def test_bucket_count_is_bounded(self):
        """结果分类必须是有限集合，否则 outcome 标签会失控。"""
        outcomes = {classify_status(c) for c in [-1, *range(100, 600)]}
        assert outcomes == {"failure", "2xx", "3xx", "4xx", "5xx"}


class TestTrackRequest:
    def test_counts_success(self, metrics: Metrics):
        with metrics.track_request("curl_cffi", "GET") as ctx:
            ctx["status_code"] = 200
            ctx["size"] = 1234

        assert _sample(metrics, "ipclick_requests_total", adapter="curl_cffi", method="GET", outcome="2xx") == 1

    def test_counts_failure(self, metrics: Metrics):
        with metrics.track_request("httpx", "POST") as ctx:
            ctx["status_code"] = -1

        assert _sample(metrics, "ipclick_requests_total", adapter="httpx", method="POST", outcome="failure") == 1

    def test_records_duration(self, metrics: Metrics):
        with metrics.track_request("curl_cffi", "GET") as ctx:
            ctx["status_code"] = 200
        assert _sample(metrics, "ipclick_request_duration_seconds_count", adapter="curl_cffi") == 1

    def test_records_size_only_when_nonzero(self, metrics: Metrics):
        with metrics.track_request("curl_cffi", "HEAD") as ctx:
            ctx["status_code"] = 200
            ctx["size"] = 0
        assert _sample(metrics, "ipclick_response_bytes_count", adapter="curl_cffi") == 0

        with metrics.track_request("curl_cffi", "GET") as ctx:
            ctx["status_code"] = 200
            ctx["size"] = 500
        assert _sample(metrics, "ipclick_response_bytes_count", adapter="curl_cffi") == 1

    def test_in_flight_returns_to_zero(self, metrics: Metrics):
        with metrics.track_request("curl_cffi", "GET") as ctx:
            assert _sample(metrics, "ipclick_requests_in_flight") == 1
            ctx["status_code"] = 200
        assert _sample(metrics, "ipclick_requests_in_flight") == 0

    def test_in_flight_decremented_on_exception(self, metrics: Metrics):
        """异常路径也必须把在途数减回去，否则这个 gauge 会一路漂高。"""
        with pytest.raises(RuntimeError), metrics.track_request("curl_cffi", "GET"):
            raise RuntimeError("boom")
        assert _sample(metrics, "ipclick_requests_in_flight") == 0

    def test_counts_recorded_even_on_exception(self, metrics: Metrics):
        with pytest.raises(RuntimeError), metrics.track_request("curl_cffi", "GET"):
            raise RuntimeError("boom")
        assert _sample(metrics, "ipclick_requests_total", adapter="curl_cffi", method="GET", outcome="failure") == 1


class TestCounters:
    def test_retry_counter(self, metrics: Metrics):
        metrics.record_retry("curl_cffi", "exception")
        metrics.record_retry("curl_cffi", "exception")
        metrics.record_retry("curl_cffi", "status_code")
        assert _sample(metrics, "ipclick_retries_total", adapter="curl_cffi", reason="exception") == 2
        assert _sample(metrics, "ipclick_retries_total", adapter="curl_cffi", reason="status_code") == 1

    def test_rejected_counter(self, metrics: Metrics):
        metrics.record_rejected("unauthenticated")
        metrics.record_rejected("url_not_allowed")
        assert _sample(metrics, "ipclick_rejected_total", reason="unauthenticated") == 1
        assert _sample(metrics, "ipclick_rejected_total", reason="url_not_allowed") == 1

    def test_build_info(self, metrics: Metrics):
        assert _sample(metrics, "ipclick_build_info", version="test") == 1


class TestLabelCardinality:
    def test_no_url_or_host_label_anywhere(self, metrics: Metrics):
        """硬规则：任何指标都不得以 URL / 主机 / 路径为标签。

        爬虫场景下这些是无界的，会把 Prometheus 撑爆；而且指标端点通常设防
        更少，暴露抓取目标等于公开业务意图。
        """
        with metrics.track_request("curl_cffi", "GET") as ctx:
            ctx["status_code"] = 200
            ctx["size"] = 10
        metrics.record_retry("curl_cffi", "exception")
        metrics.record_rejected("unauthenticated")

        forbidden = {"url", "host", "hostname", "target", "path", "domain", "uri"}
        assert metrics.registry is not None
        for metric in metrics.registry.collect():
            for sample in metric.samples:
                offending = forbidden & set(sample.labels)
                assert not offending, f"{sample.name} 含有高基数标签: {offending}"

    def test_label_names_are_the_expected_finite_set(self, metrics: Metrics):
        with metrics.track_request("curl_cffi", "GET") as ctx:
            ctx["status_code"] = 200
        metrics.record_retry("curl_cffi", "exception")
        metrics.record_rejected("x")

        allowed = {"adapter", "method", "outcome", "reason", "version", "le", "quantile"}
        assert metrics.registry is not None
        for metric in metrics.registry.collect():
            for sample in metric.samples:
                assert set(sample.labels) <= allowed, f"{sample.name} 有意料之外的标签: {set(sample.labels)}"


class TestSingleton:
    def test_get_metrics_is_singleton(self):
        reset_metrics()
        try:
            assert get_metrics() is get_metrics()
        finally:
            reset_metrics()


class TestHttpServer:
    def test_serves_metrics(self, metrics: Metrics):
        import urllib.request

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = int(s.getsockname()[1])

        assert metrics.start_http_server(port, "127.0.0.1")
        with metrics.track_request("curl_cffi", "GET") as ctx:
            ctx["status_code"] = 200

        # 指标端点是本机 HTTP，别让环境代理把它劫走
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        body = opener.open(f"http://127.0.0.1:{port}/metrics", timeout=5).read().decode()

        assert "ipclick_requests_total" in body
        assert 'adapter="curl_cffi"' in body

    def test_second_start_is_idempotent(self, metrics: Metrics):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = int(s.getsockname()[1])
        assert metrics.start_http_server(port, "127.0.0.1")
        assert metrics.start_http_server(port, "127.0.0.1")

    def test_port_conflict_returns_false_instead_of_crashing(self, metrics: Metrics):
        """端口被占用不应该让服务端启动失败——指标是可观测性，不是核心功能。"""
        holder = socket.socket()
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        try:
            assert metrics.start_http_server(int(holder.getsockname()[1]), "127.0.0.1") is False
        finally:
            holder.close()


class TestGracefulDegradation:
    def test_noop_metric_absorbs_everything(self):
        """未安装 prometheus_client 时的占位符必须吞掉所有调用。"""
        from ipclick.metrics import _NoopMetric

        noop = _NoopMetric()
        noop.labels(a="b").inc()
        noop.labels(a="b").observe(1.0)
        noop.inc()
        noop.dec()
        noop.set(1)
        noop.info({"k": "v"})

    def test_metrics_object_works_without_prometheus(self, monkeypatch: pytest.MonkeyPatch):
        """模拟未安装场景：埋点照常调用，不抛异常。

        这里 patch 的是 `_prom` 而不是 METRICS_AVAILABLE——代码里所有分支都从
        `_prom` 判断，patch 那个标志位测不到真正的降级路径。
        """
        monkeypatch.setattr("ipclick.metrics._prom", None)
        m = Metrics()
        assert m.enabled is False
        assert m.registry is None

        with m.track_request("curl_cffi", "GET") as ctx:
            ctx["status_code"] = 200
            ctx["size"] = 100
        m.record_retry("curl_cffi", "exception")
        m.record_rejected("unauthenticated")
        assert m.start_http_server(1) is False


class TestServiceIntegration:
    def test_task_service_records_request(self, monkeypatch: pytest.MonkeyPatch):
        from ipclick.dto.proto import task_pb2
        from ipclick.services.task_service import TaskService
        from ipclick.utils.config_util import Settings
        from tests.test_task_service import FakeContext, RecordingAdapter

        adapter = RecordingAdapter()
        monkeypatch.setattr("ipclick.services.task_service.get_default_adapter", lambda settings=None: adapter)
        monkeypatch.setattr("ipclick.services.task_service.get_adapter", lambda name, settings=None: adapter)

        from prometheus_client import CollectorRegistry

        own = Metrics(registry=CollectorRegistry(), version="test")
        monkeypatch.setattr("ipclick.services.task_service.get_metrics", lambda: own)

        service = TaskService(Settings({}))
        service.Send(task_pb2.ReqTask(url="http://example.com", uuid="u1"), FakeContext())

        assert _sample(own, "ipclick_requests_total", adapter="curl_cffi", method="GET", outcome="2xx") == 1

    def test_blocked_url_recorded_as_rejected(self, monkeypatch: pytest.MonkeyPatch):
        from ipclick.dto.proto import task_pb2
        from ipclick.services.task_service import TaskService
        from ipclick.utils.config_util import Settings
        from tests.test_task_service import FakeContext, RecordingAdapter

        adapter = RecordingAdapter()
        monkeypatch.setattr("ipclick.services.task_service.get_default_adapter", lambda settings=None: adapter)
        monkeypatch.setattr("ipclick.services.task_service.get_adapter", lambda name, settings=None: adapter)

        from prometheus_client import CollectorRegistry

        own = Metrics(registry=CollectorRegistry(), version="test")
        monkeypatch.setattr("ipclick.services.task_service.get_metrics", lambda: own)

        service = TaskService(Settings({"SECURITY": {"block_private_networks": True}}))
        service.Send(task_pb2.ReqTask(url="http://127.0.0.1:8000/", uuid="u1"), FakeContext())

        assert _sample(own, "ipclick_rejected_total", reason="url_not_allowed") == 1

    def test_adapter_label_is_resolved_not_unknown(self, monkeypatch: pytest.MonkeyPatch):
        """回归：适配器名若在 track_request 之后才解析，所有指标都会记成 unknown。"""
        from ipclick.dto.models import IPClickAdapter
        from ipclick.dto.proto import task_pb2
        from ipclick.services.task_service import TaskService
        from ipclick.utils.config_util import Settings
        from tests.test_task_service import FakeContext, RecordingAdapter

        adapter = RecordingAdapter()
        adapter.adapter_name = "httpx"
        monkeypatch.setattr("ipclick.services.task_service.get_default_adapter", lambda settings=None: adapter)
        monkeypatch.setattr("ipclick.services.task_service.get_adapter", lambda name, settings=None: adapter)

        from prometheus_client import CollectorRegistry

        own = Metrics(registry=CollectorRegistry(), version="test")
        monkeypatch.setattr("ipclick.services.task_service.get_metrics", lambda: own)

        service = TaskService(Settings({}))
        service.Send(
            task_pb2.ReqTask(url="http://example.com", uuid="u1", adapter=IPClickAdapter.HTTPX.pb_value),
            FakeContext(),
        )

        assert _sample(own, "ipclick_requests_total", adapter="httpx", method="GET", outcome="2xx") == 1
        assert _sample(own, "ipclick_requests_total", adapter="unknown", method="GET", outcome="2xx") == 0
