"""客户端到服务端这一跳的重试。

适配器内部的重试解决的是"目标站点抖了"，这里解决的是"我们自己的服务端抖了"，
两者互不覆盖。

最要紧的一条：**只重试确定没到达服务端的请求**。
"""

import time

import grpc
import pytest

from ipclick.dto.models import DownloadTask
from ipclick.exceptions import TransportError
from ipclick.sdk import Downloader
from tests.test_sdk_e2e import _free_port


class _FakeRpcError(grpc.RpcError):
    def __init__(self, code: grpc.StatusCode, details: str = "boom"):
        self._code = code
        self._details = details

    def code(self) -> grpc.StatusCode:
        return self._code

    def details(self) -> str:
        return self._details


def _client(**kwargs) -> Downloader:
    return Downloader(host="127.0.0.1", port=_free_port(), **kwargs)


class TestConfig:
    def test_defaults(self):
        with _client() as d:
            assert d.rpc_max_retries == 2
            assert d.rpc_retry_backoff == 0.5

    def test_reads_config(self, tmp_path):
        from ipclick.config_loader.loader import load_config

        cfg = tmp_path / "c.toml"
        cfg.write_text("[CLIENT]\nrpc_max_retries = 5\nrpc_retry_backoff = 0.1\n", encoding="utf-8")
        load_config.cache_clear()
        try:
            with Downloader(config_path=str(cfg), host="127.0.0.1", port=_free_port()) as d:
                assert d.rpc_max_retries == 5
                assert d.rpc_retry_backoff == 0.1
        finally:
            load_config.cache_clear()

    def test_bad_values_fall_back(self, tmp_path):
        from ipclick.config_loader.loader import load_config

        cfg = tmp_path / "c.toml"
        cfg.write_text('[CLIENT]\nrpc_max_retries = "abc"\nrpc_retry_backoff = -1\n', encoding="utf-8")
        load_config.cache_clear()
        try:
            with Downloader(config_path=str(cfg), host="127.0.0.1", port=_free_port()) as d:
                assert (d.rpc_max_retries, d.rpc_retry_backoff) == (2, 0.5)
        finally:
            load_config.cache_clear()


class TestRetryDecision:
    def test_unavailable_is_retried(self):
        """UNAVAILABLE = 连接压根没建起来，请求没到过服务端，重发是安全的。"""
        with _client() as d:
            assert d._should_retry_rpc(_FakeRpcError(grpc.StatusCode.UNAVAILABLE), 0) is True

    def test_deadline_exceeded_is_not_retried(self):
        """核心安全性：请求可能已经在服务端执行了，只是回复没赶上。

        这时重发一个 POST 就是重复下单。宁可让调用方拿到超时自己决定。
        """
        with _client() as d:
            assert d._should_retry_rpc(_FakeRpcError(grpc.StatusCode.DEADLINE_EXCEEDED), 0) is False

    @pytest.mark.parametrize(
        "code",
        [
            grpc.StatusCode.INVALID_ARGUMENT,
            grpc.StatusCode.PERMISSION_DENIED,
            grpc.StatusCode.UNAUTHENTICATED,
            grpc.StatusCode.FAILED_PRECONDITION,
            grpc.StatusCode.RESOURCE_EXHAUSTED,
            grpc.StatusCode.INTERNAL,
        ],
    )
    def test_other_codes_are_not_retried(self, code: grpc.StatusCode):
        """这些要么是调用方的错、要么服务端已经处理过了，重试都没有意义。"""
        with _client() as d:
            assert d._should_retry_rpc(_FakeRpcError(code), 0) is False

    def test_stops_at_max(self):
        with _client() as d:
            err = _FakeRpcError(grpc.StatusCode.UNAVAILABLE)
            assert d._should_retry_rpc(err, d.rpc_max_retries - 1) is True
            assert d._should_retry_rpc(err, d.rpc_max_retries) is False

    def test_zero_retries_disables(self, tmp_path):
        from ipclick.config_loader.loader import load_config

        cfg = tmp_path / "c.toml"
        cfg.write_text("[CLIENT]\nrpc_max_retries = 0\n", encoding="utf-8")
        load_config.cache_clear()
        try:
            with Downloader(config_path=str(cfg), host="127.0.0.1", port=_free_port()) as d:
                assert d._should_retry_rpc(_FakeRpcError(grpc.StatusCode.UNAVAILABLE), 0) is False
        finally:
            load_config.cache_clear()


class TestActualRetry:
    def test_unreachable_server_retries_then_fails(self, tmp_path):
        """连不上的服务端会被重试若干次，最终仍抛 TransportError。"""
        from ipclick.config_loader.loader import load_config

        cfg = tmp_path / "c.toml"
        cfg.write_text("[CLIENT]\nrpc_max_retries = 2\nrpc_retry_backoff = 0.05\n", encoding="utf-8")
        load_config.cache_clear()
        try:
            with Downloader(config_path=str(cfg), host="127.0.0.1", port=_free_port()) as d:
                start = time.monotonic()
                with pytest.raises(TransportError):
                    d.download(DownloadTask(url="http://example.com/x", timeout=1, max_retries=0))
                elapsed = time.monotonic() - start
            # 0.05 + 0.10 = 0.15 秒的退避，加上三次连接尝试
            assert elapsed >= 0.15, f"没有退避重试，只用了 {elapsed:.3f}s"
        finally:
            load_config.cache_clear()

    def test_no_retry_is_faster(self, tmp_path):
        from ipclick.config_loader.loader import load_config

        cfg = tmp_path / "c.toml"
        cfg.write_text("[CLIENT]\nrpc_max_retries = 0\n", encoding="utf-8")
        load_config.cache_clear()
        try:
            with Downloader(config_path=str(cfg), host="127.0.0.1", port=_free_port()) as d:
                start = time.monotonic()
                with pytest.raises(TransportError):
                    d.download(DownloadTask(url="http://example.com/x", timeout=1, max_retries=0))
                assert time.monotonic() - start < 1.5
        finally:
            load_config.cache_clear()

    def test_request_still_returns_error_response(self, tmp_path):
        """重试用尽后 request() 仍按契约返回 -1 响应，而不是抛异常。"""
        from ipclick.config_loader.loader import load_config

        cfg = tmp_path / "c.toml"
        cfg.write_text("[CLIENT]\nrpc_max_retries = 1\nrpc_retry_backoff = 0.05\n", encoding="utf-8")
        load_config.cache_clear()
        try:
            with Downloader(config_path=str(cfg), host="127.0.0.1", port=_free_port()) as d:
                resp = d.get("http://example.com/x", timeout=1, max_retries=0)
            assert resp.status_code == -1
            assert resp.error
        finally:
            load_config.cache_clear()
