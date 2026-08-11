"""niquests 适配器专属测试。

与 curl_cffi / httpx 的行为一致性由 ``test_adapters.py`` 的参数化用例覆盖，
这里只测 niquests 特有的部分：可选依赖、Session 配置、真流式。
"""

from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
import socket
import threading

import pytest

from ipclick.adapters import registry
from ipclick.adapters.base import StreamHeader
from ipclick.adapters.niquests_adapter import NIQUESTS_AVAILABLE, NiquestsAdapter
from ipclick.adapters.settings import AdapterSettings
from ipclick.exceptions import AdapterError


pytestmark = pytest.mark.skipif(not NIQUESTS_AVAILABLE, reason="niquests 未安装")

#: 流式服务分 8 段发送，每段这么长
_CHUNK = b"x" * 4096
_CHUNK_COUNT = 8


class _StreamHandler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(_CHUNK) * _CHUNK_COUNT))
        self.end_headers()
        for _ in range(_CHUNK_COUNT):
            self.wfile.write(_CHUNK)
            self.wfile.flush()


@pytest.fixture(scope="module")
def stream_server() -> Iterator[str]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])

    server = HTTPServer(("127.0.0.1", port), _StreamHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def adapter() -> Iterator[NiquestsAdapter]:
    instance = NiquestsAdapter()
    try:
        yield instance
    finally:
        instance.close()


class TestOptionalDependency:
    def test_registered_when_installed(self):
        assert registry.ADAPTER_CLASSES.get("niquests") is NiquestsAdapter

    def test_constructing_without_requests_raises_with_install_hint(self, monkeypatch: pytest.MonkeyPatch):
        """缺依赖时的报错必须说清楚"怎么装"，而不是让人去读源码。"""
        monkeypatch.setattr("ipclick.adapters.niquests_adapter._niquests", None)
        with pytest.raises(AdapterError, match=r"ipclick\[niquests\]"):
            NiquestsAdapter()

    def test_registry_hint_when_not_registered(self, monkeypatch: pytest.MonkeyPatch):
        """没装 niquests 时，get_adapter 的报错应该是"缺依赖"而不是"尚未支持"——
        两者的处理方式完全不同：一个 pip install 能解决，一个不能。"""
        monkeypatch.delitem(registry.ADAPTER_CLASSES, "niquests", raising=False)
        with pytest.raises(AdapterError, match=r"需要额外依赖.*ipclick\[niquests\]"):
            registry.get_adapter("niquests")


class TestSessionConfiguration:
    def test_does_not_trust_env_by_default(self, adapter: NiquestsAdapter):
        """niquests（同 requests）默认 trust_env=True 会捡起环境里的 HTTP_PROXY——
        对一个"代发请求"的服务来说，这意味着出口 IP 悄悄变了。"""
        assert adapter.trust_env is False
        assert adapter._get_session(None, True).trust_env is False

    def test_explicit_proxy_is_set(self, adapter: NiquestsAdapter):
        session = adapter._get_session("http://127.0.0.1:9", True)
        assert session.proxies == {"http": "http://127.0.0.1:9", "https": "http://127.0.0.1:9"}

    def test_separate_session_per_proxy_and_verify(self, adapter: NiquestsAdapter):
        adapter._get_session(None, True)
        adapter._get_session(None, False)
        adapter._get_session("http://127.0.0.1:9", True)
        assert len(adapter._sessions) == 3

    def test_verify_flag_applied(self, adapter: NiquestsAdapter):
        assert adapter._get_session(None, False).verify is False

    def test_transport_retries_disabled(self, adapter: NiquestsAdapter):
        """urllib3 自带重试若不关掉，会和本项目的 retry 装饰器叠乘：
        配 3 次重试实际发 9 次请求，退避时间也完全不受控。"""
        session = adapter._get_session(None, True)
        for prefix in ("http://", "https://"):
            assert session.adapters[prefix].max_retries.total == 0

    def test_pool_size_from_settings(self):
        adapter = NiquestsAdapter(AdapterSettings(max_connections=7, max_keepalive_connections=3))
        try:
            pool = adapter._get_session(None, True).adapters["https://"].poolmanager.connection_pool_kw
            assert pool["maxsize"] == 7
        finally:
            adapter.close()

    def test_timeout_is_split_into_connect_and_read(self, adapter: NiquestsAdapter):
        """niquests 传单值 timeout 时连接和读取共用一个值，
        [DOWNLOADER].connect_timeout 就形同虚设了。"""
        built = adapter._request_kwargs({"timeout": 30}, {})
        assert built["timeout"] == (adapter.settings.connect_timeout, 30)

    def test_zero_timeout_falls_back_to_default(self, adapter: NiquestsAdapter):
        built = adapter._request_kwargs({"timeout": 0}, {})
        assert built["timeout"] == (adapter.settings.connect_timeout, adapter.timeout)


class TestStreaming:
    def _collect(self, events: Iterator[object]) -> tuple[StreamHeader, bytes]:
        header = next(events)
        assert isinstance(header, StreamHeader)
        body = b"".join(e for e in events if isinstance(e, bytes))
        return header, body

    def test_header_arrives_before_body(self, adapter: NiquestsAdapter, stream_server: str):
        events = adapter.download_stream(f"{stream_server}/big", method="GET")
        first = next(events)
        assert isinstance(first, StreamHeader)
        assert first.status_code == 200
        assert first.content_length == len(_CHUNK) * _CHUNK_COUNT
        events.close()

    def test_body_reassembles_exactly(self, adapter: NiquestsAdapter, stream_server: str):
        _, body = self._collect(adapter.download_stream(f"{stream_server}/big", method="GET"))
        assert body == _CHUNK * _CHUNK_COUNT

    def test_arrives_in_multiple_chunks(self, adapter: NiquestsAdapter, stream_server: str):
        """真流式的意义就在这里：不是一次性读完再切片。"""
        events = adapter.download_stream(f"{stream_server}/big", method="GET", chunk_size=4096)
        next(events)
        assert sum(1 for e in events if isinstance(e, bytes)) > 1

    def test_unreachable_server_yields_error_header(self, adapter: NiquestsAdapter):
        """建流失败不能抛异常——调用方拿到的是生成器，异常会在意想不到的地方炸。"""
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            dead = int(s.getsockname()[1])

        header = next(adapter.download_stream(f"http://127.0.0.1:{dead}/", method="GET"))
        assert isinstance(header, StreamHeader)
        assert header.status_code == -1
        assert header.error

    def test_bad_method_yields_error_header(self, adapter: NiquestsAdapter, stream_server: str):
        header = next(adapter.download_stream(f"{stream_server}/x", method="BREW"))
        assert isinstance(header, StreamHeader)
        assert header.status_code == -1
        assert "BREW" in (header.error or "")

    def test_early_close_does_not_leak(self, adapter: NiquestsAdapter, stream_server: str):
        """调用方只要前几个字节就走人是常态，底层连接必须被释放。"""
        events = adapter.download_stream(f"{stream_server}/big", method="GET")
        next(events)
        next(events)
        events.close()  # 触发 finally 里的 resp.close()
