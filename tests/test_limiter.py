"""按 host 的并发与速率限制。

时间敏感的用例都用宽松的边界：CI 机器负载不定，卡得太死会变成随机失败的测试。
"""

from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from ipclick.limiter import HostLimiter, HostLimitTimeout, LimiterSettings, host_of


class TestHostOf:
    def test_plain(self):
        assert host_of("https://Example.COM/path") == "example.com"

    def test_port_is_dropped(self):
        """example.com:8080 和 example.com:443 通常是同一台机器，
        分开计数就限不住了。"""
        assert host_of("http://example.com:8080/a") == host_of("https://example.com/b")

    def test_ipv6(self):
        assert host_of("http://[::1]:9527/x") == "::1"

    def test_no_host(self):
        assert host_of("not-a-url") == ""
        assert host_of("") == ""

    def test_malformed_does_not_raise(self):
        assert host_of("http://[oops") == ""


class TestSettings:
    def test_disabled_by_default(self):
        s = LimiterSettings.from_config({})
        assert not s.enabled

    def test_enabled_by_either_knob(self):
        assert LimiterSettings.from_config({"concurrency": {"per_host_max_concurrent": 2}}).enabled
        assert LimiterSettings.from_config({"rate_limit": {"per_host_qps": 1}}).enabled

    def test_parses_both_sections(self):
        s = LimiterSettings.from_config(
            {
                "concurrency": {
                    "per_host_max_concurrent": 3,
                    "per_host_wait_timeout": 5,
                    "per_host_idle_ttl": 60,
                    "max_tracked_hosts": 128,
                },
                "rate_limit": {"per_host_qps": 2.5, "per_host_burst": 7},
            }
        )
        assert (s.per_host_max_concurrent, s.wait_timeout) == (3, 5.0)
        assert (s.per_host_qps, s.per_host_burst, s.burst) == (2.5, 7, 7)
        assert (s.idle_ttl, s.max_tracked_hosts) == (60.0, 128)

    def test_burst_defaults_to_one_second_of_tokens(self):
        assert LimiterSettings(per_host_qps=2.5).burst == 3
        assert LimiterSettings(per_host_qps=0.5).burst == 1

    def test_burst_is_zero_when_rate_limiting_off(self):
        assert LimiterSettings().burst == 0

    def test_bad_values_fall_back(self):
        s = LimiterSettings.from_config(
            {
                "concurrency": {"per_host_max_concurrent": "abc", "max_tracked_hosts": 2},
                "rate_limit": {"per_host_qps": -1},
            }
        )
        assert s.per_host_max_concurrent == 0
        assert s.per_host_qps == 0.0
        assert s.max_tracked_hosts == 10_000, "低于下限的值应回落，否则会疯狂淘汰"


class TestDisabled:
    def test_no_limits_means_no_op(self):
        limiter = HostLimiter(LimiterSettings())
        with limiter.acquire("http://example.com/x"):
            pass
        assert limiter.snapshot()["tracked_hosts"] == 0, "未启用时不该产生任何条目"

    def test_url_without_host_is_not_limited(self):
        """URL 非法会在 validate_url 那层被拦下，限流器不该在这里炸。"""
        limiter = HostLimiter(LimiterSettings(per_host_max_concurrent=1))
        with limiter.acquire("garbage"):
            pass


class TestConcurrency:
    def test_strictly_caps_in_flight(self):
        """核心保证：同一 host 的在途请求数任何时刻都不超过上限。"""
        limiter = HostLimiter(LimiterSettings(per_host_max_concurrent=3, wait_timeout=10))
        peak = 0
        current = 0
        lock = threading.Lock()

        def work(_i: int) -> None:
            nonlocal peak, current
            with limiter.acquire("http://example.com/x"):
                with lock:
                    current += 1
                    peak = max(peak, current)
                time.sleep(0.02)
                with lock:
                    current -= 1

        with ThreadPoolExecutor(16) as pool:
            list(pool.map(work, range(32)))

        assert peak <= 3, f"并发峰值 {peak} 超过了上限 3"
        assert peak > 1, "如果峰值恒为 1，说明测试根本没并发起来，限制形同虚设"

    def test_different_hosts_do_not_block_each_other(self):
        """按 host 独立计数——否则就成了全局限流。"""
        limiter = HostLimiter(LimiterSettings(per_host_max_concurrent=1, wait_timeout=5))
        started = threading.Event()
        release = threading.Event()

        def hold() -> None:
            with limiter.acquire("http://a.com/x"):
                started.set()
                release.wait(timeout=5)

        t = threading.Thread(target=hold, daemon=True)
        t.start()
        assert started.wait(timeout=5)
        try:
            # a.com 的唯一额度被占着，b.com 应当畅通
            start = time.monotonic()
            with limiter.acquire("http://b.com/x"):
                pass
            assert time.monotonic() - start < 1.0
        finally:
            release.set()
            t.join(timeout=5)

    def test_timeout_raises_instead_of_hanging(self):
        """等不到额度必须失败退出——不能无限期占着 gRPC worker 线程。"""
        limiter = HostLimiter(LimiterSettings(per_host_max_concurrent=1, wait_timeout=0.2))
        with (
            limiter.acquire("http://a.com/x"),
            pytest.raises(HostLimitTimeout, match="并发额度"),
            limiter.acquire("http://a.com/y"),
        ):
            pass

    def test_slot_released_on_exception(self):
        """请求抛异常时额度必须归还，否则几次失败就把 host 永久锁死。"""
        limiter = HostLimiter(LimiterSettings(per_host_max_concurrent=1, wait_timeout=0.5))
        for _ in range(3):
            with pytest.raises(RuntimeError), limiter.acquire("http://a.com/x"):
                raise RuntimeError("boom")
        # 还能正常拿到
        with limiter.acquire("http://a.com/x"):
            pass

    def test_snapshot_reports_active(self):
        limiter = HostLimiter(LimiterSettings(per_host_max_concurrent=2))
        with limiter.acquire("http://a.com/x"):
            snap = limiter.snapshot()
            assert snap["active"] == {"a.com": 1}
            assert snap["tracked_hosts"] == 1


class TestRateLimit:
    def test_paces_requests(self):
        """10 QPS、桶容量 1：4 个请求至少要花 3 个间隔。"""
        limiter = HostLimiter(LimiterSettings(per_host_qps=10, per_host_burst=1, wait_timeout=5))
        start = time.monotonic()
        for _ in range(4):
            with limiter.acquire("http://a.com/x"):
                pass
        elapsed = time.monotonic() - start
        assert elapsed >= 0.25, f"4 个请求只用了 {elapsed:.3f}s，限速没生效"

    def test_burst_allowed_upfront(self):
        """桶初始装满：服务刚起来的第一批请求不该被无谓地拖慢。"""
        limiter = HostLimiter(LimiterSettings(per_host_qps=1, per_host_burst=5, wait_timeout=5))
        start = time.monotonic()
        for _ in range(5):
            with limiter.acquire("http://a.com/x"):
                pass
        assert time.monotonic() - start < 0.5

    def test_timeout_when_rate_too_low(self):
        limiter = HostLimiter(LimiterSettings(per_host_qps=0.5, per_host_burst=1, wait_timeout=0.2))
        with limiter.acquire("http://a.com/x"):
            pass
        with pytest.raises(HostLimitTimeout, match="速率额度"), limiter.acquire("http://a.com/x"):
            pass

    def test_separate_buckets_per_host(self):
        limiter = HostLimiter(LimiterSettings(per_host_qps=1, per_host_burst=1, wait_timeout=0.2))
        with limiter.acquire("http://a.com/x"):
            pass
        # b.com 有自己的桶，不该受影响
        with limiter.acquire("http://b.com/x"):
            pass


class TestServerIntegration:
    """闸门必须真的挡在下载路径上——单测限流器本身通过、但没接进去的话等于没做。"""

    def _service(self, monkeypatch: pytest.MonkeyPatch, downloader_config: dict) -> tuple:
        from concurrent import futures
        from typing import Any

        import grpc

        from ipclick.adapters.base import DownloaderAdapter
        from ipclick.dto.proto import task_pb2_grpc
        from ipclick.dto.response import Response
        from ipclick.services.task_service import TaskService
        from ipclick.utils.config_util import Settings
        from tests.test_sdk_e2e import _free_port

        state = {"current": 0, "peak": 0}
        lock = threading.Lock()

        class SlowAdapter(DownloaderAdapter):
            adapter_name = "curl_cffi"

            def download(self, url: str, **kwargs: Any) -> Response:  # type: ignore[override]
                with lock:
                    state["current"] += 1
                    state["peak"] = max(state["peak"], state["current"])
                time.sleep(0.05)
                with lock:
                    state["current"] -= 1
                return Response(url=url, status_code=200, content=b"ok")

        adapter = SlowAdapter()
        monkeypatch.setattr("ipclick.services.task_service.get_default_adapter", lambda settings=None: adapter)
        monkeypatch.setattr(
            "ipclick.services.task_service.get_adapter",
            lambda name, settings=None, browser_settings=None: adapter,
        )

        service = TaskService(Settings({"DOWNLOADER": downloader_config, "SERVER": {"max_workers": 16}}))
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=16))
        task_pb2_grpc.add_TaskServiceServicer_to_server(service, server)
        port = _free_port()
        server.add_insecure_port(f"127.0.0.1:{port}")
        server.start()
        return port, server, service, state

    def test_limit_applies_through_grpc(self, monkeypatch: pytest.MonkeyPatch):
        from ipclick.sdk import Downloader

        port, server, service, state = self._service(
            monkeypatch, {"concurrency": {"per_host_max_concurrent": 2, "per_host_wait_timeout": 20}}
        )
        try:
            with Downloader(host="127.0.0.1", port=port) as d, ThreadPoolExecutor(10) as pool:
                results = list(pool.map(lambda i: d.get(f"http://target.example/{i}"), range(20)))
            assert all(r.status_code == 200 for r in results)
            assert state["peak"] <= 2, f"服务端并发峰值 {state['peak']} 超过了 per_host_max_concurrent=2"
            assert state["peak"] > 1, "峰值恒为 1 说明并发根本没起来，这个断言就没意义了"
        finally:
            server.stop(grace=0).wait(timeout=5)
            service.cleanup()

    def test_throttle_timeout_surfaces_as_host_limit_error(self, monkeypatch: pytest.MonkeyPatch):
        """限流超时不是网络故障，不该被吞成 -1 响应让人去查网络。"""
        from ipclick.sdk import Downloader

        port, server, service, _ = self._service(
            monkeypatch, {"concurrency": {"per_host_max_concurrent": 1, "per_host_wait_timeout": 0.01}}
        )
        try:
            with Downloader(host="127.0.0.1", port=port) as d, ThreadPoolExecutor(8) as pool:
                outcomes = list(
                    pool.map(
                        lambda i: _capture(lambda: d.get(f"http://target.example/{i}")),
                        range(8),
                    )
                )
            throttled = [o for o in outcomes if isinstance(o, HostLimitTimeout)]
            assert throttled, f"没有任何请求被限流，实际结果: {outcomes[:3]}"
            assert "限流" in str(throttled[0])
        finally:
            server.stop(grace=0).wait(timeout=5)
            service.cleanup()

    def test_different_hosts_unaffected_through_grpc(self, monkeypatch: pytest.MonkeyPatch):
        from ipclick.sdk import Downloader

        port, server, service, state = self._service(
            monkeypatch, {"concurrency": {"per_host_max_concurrent": 1, "per_host_wait_timeout": 20}}
        )
        try:
            with Downloader(host="127.0.0.1", port=port) as d, ThreadPoolExecutor(8) as pool:
                results = list(pool.map(lambda i: d.get(f"http://host{i}.example/x"), range(8)))
            assert all(r.status_code == 200 for r in results)
            assert state["peak"] > 1, "不同 host 之间不该互相阻塞"
        finally:
            server.stop(grace=0).wait(timeout=5)
            service.cleanup()


def _capture(fn):
    try:
        return fn()
    except Exception as e:
        return e


class TestEviction:
    def test_idle_hosts_are_reclaimed(self):
        """爬虫会碰到无穷多域名，不回收就是一条稳定的内存泄漏。"""
        limiter = HostLimiter(LimiterSettings(per_host_max_concurrent=1, idle_ttl=1.0, max_tracked_hosts=16))
        for i in range(20):
            with limiter.acquire(f"http://h{i}.com/x"):
                pass
        # 触及上限时会强制清理空闲条目
        assert limiter.snapshot()["tracked_hosts"] < 20

    def test_active_hosts_are_never_evicted(self):
        """正在用的条目被回收 = 它的并发计数归零 = 限制被绕过。"""
        limiter = HostLimiter(LimiterSettings(per_host_max_concurrent=1, idle_ttl=0.0, max_tracked_hosts=4))
        held = threading.Event()
        release = threading.Event()

        def hold() -> None:
            with limiter.acquire("http://keep.com/x"):
                held.set()
                release.wait(timeout=5)

        t = threading.Thread(target=hold, daemon=True)
        t.start()
        assert held.wait(timeout=5)
        try:
            for i in range(12):
                with limiter.acquire(f"http://other{i}.com/x"):
                    pass
            assert "keep.com" in limiter.snapshot()["active"]
        finally:
            release.set()
            t.join(timeout=5)
