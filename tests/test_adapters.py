"""适配器测试：对本机起的一个小 HTTP 服务发真实请求（不出网）。"""

from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import socket
import threading

import pytest

from ipclick.adapters.curl_cffi_adapter import CURL_CFFI_AVAILABLE, CurlCffiAdapter
from ipclick.adapters.httpx_adapter import HTTPX_AVAILABLE, HttpxAdapter
from ipclick.exceptions import AdapterError


class _Handler(BaseHTTPRequestHandler):
    """把收到的请求回显成 JSON。"""

    def log_message(self, *args: object) -> None:  # 静音访问日志
        pass

    def _respond(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""

        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "/landed")
            self.end_headers()
            return

        if self.path.startswith("/status/"):
            self.send_response(int(self.path.rsplit("/", 1)[-1]))
            self.end_headers()
            return

        payload = json.dumps(
            {
                "method": self.command,
                "path": self.path,
                "headers": dict(self.headers),
                "body": body,
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _respond


@pytest.fixture(scope="module")
def http_server() -> Iterator[str]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])

    server = HTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _pool_of(adapter: object) -> dict:
    """取适配器的连接池字典（curl_cffi 叫 _sessions，httpx 叫 _clients）。

    注意不能写成 ``_sessions or _clients``——池为空时是 falsy，会误取到另一个。
    """
    pool = getattr(adapter, "_sessions", None)
    return pool if pool is not None else adapter._clients  # type: ignore[attr-defined]


@pytest.fixture(params=["curl_cffi", "httpx"])
def adapter(request: pytest.FixtureRequest) -> Iterator[object]:
    if request.param == "curl_cffi":
        if not CURL_CFFI_AVAILABLE:
            pytest.skip("curl_cffi 未安装")
        instance = CurlCffiAdapter()
    else:
        if not HTTPX_AVAILABLE:
            pytest.skip("httpx 未安装")
        instance = HttpxAdapter()
    try:
        yield instance
    finally:
        instance.close()


class TestAdapterBehaviour:
    """两个适配器必须表现一致——过去 httpx 会静默丢掉一堆参数。"""

    def test_basic_get(self, adapter, http_server: str):
        resp = adapter.download(f"{http_server}/hello", method="GET", kwargs="{}")
        assert resp.status_code == 200
        assert resp.json()["method"] == "GET"

    def test_json_body_is_sent(self, adapter, http_server: str):
        """回归：httpx 适配器完全忽略 json 参数，请求体直接丢失。"""
        resp = adapter.download(f"{http_server}/x", method="POST", json={"ping": "pong"}, kwargs="{}")
        assert json.loads(resp.json()["body"]) == {"ping": "pong"}

    def test_form_data_is_sent(self, adapter, http_server: str):
        resp = adapter.download(f"{http_server}/x", method="POST", data={"foo": "bar"}, kwargs="{}")
        assert "foo=bar" in resp.json()["body"]

    def test_custom_headers_sent(self, adapter, http_server: str):
        resp = adapter.download(f"{http_server}/x", method="GET", headers={"X-Demo": "ipclick"}, kwargs="{}")
        assert resp.json()["headers"].get("X-Demo") == "ipclick"

    def test_default_user_agent_present(self, adapter, http_server: str):
        resp = adapter.download(f"{http_server}/x", method="GET", kwargs="{}")
        assert resp.json()["headers"].get("User-Agent")

    def test_query_params(self, adapter, http_server: str):
        resp = adapter.download(f"{http_server}/x", method="GET", params={"a": "1"}, kwargs="{}")
        assert "a=1" in resp.json()["path"]

    def test_allow_redirects_true_follows(self, adapter, http_server: str):
        resp = adapter.download(f"{http_server}/redirect", method="GET", allow_redirects=True, kwargs="{}")
        assert resp.status_code == 200
        assert resp.json()["path"] == "/landed"

    def test_allow_redirects_false_stops(self, adapter, http_server: str):
        """回归：httpx 适配器忽略 allow_redirects，永远跟随重定向。"""
        resp = adapter.download(f"{http_server}/redirect", method="GET", allow_redirects=False, kwargs="{}")
        assert resp.status_code == 302

    def test_empty_kwargs_string_does_not_crash(self, adapter, http_server: str):
        """回归：curl_cffi 适配器无条件 json.loads(kwargs)，空串直接 JSONDecodeError。"""
        resp = adapter.download(f"{http_server}/x", method="GET", kwargs="")
        assert resp.status_code == 200

    def test_none_kwargs_does_not_crash(self, adapter, http_server: str):
        resp = adapter.download(f"{http_server}/x", method="GET", kwargs=None)
        assert resp.status_code == 200

    def test_session_is_reused(self, adapter, http_server: str):
        """回归：get_session() 从没被调用，每次请求都新建连接。"""
        adapter.download(f"{http_server}/1", method="GET", kwargs="{}")
        pool = _pool_of(adapter)
        assert len(pool) == 1
        adapter.download(f"{http_server}/2", method="GET", kwargs="{}")
        assert len(pool) == 1

    def test_close_clears_pool(self, adapter, http_server: str):
        adapter.download(f"{http_server}/1", method="GET", kwargs="{}")
        adapter.close()
        pool = _pool_of(adapter)
        assert len(pool) == 0

    def test_connection_error_yields_error_response(self, adapter):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            dead_port = int(s.getsockname()[1])

        resp = adapter.download(f"http://127.0.0.1:{dead_port}/", method="GET", max_retries=0, kwargs="{}")
        assert resp.status_code == -1
        assert resp.exception is not None

    def test_stream_true_does_not_silently_drop_the_body(self, adapter, http_server: str):
        """回归：curl_cffi 在 stream=True 时返回未消费的流式响应，读 .content 得到 b''，
        而 status_code 仍是 200、exception 仍是 None——调用方完全无从察觉响应体已丢失。
        两个适配器都必须忽略该参数（真正的流式传输见 README「尚未实现」）。"""
        resp = adapter.download(f"{http_server}/x", method="GET", stream=True, kwargs="{}")
        assert resp.status_code == 200
        assert resp.content, "stream=True 时响应体被静默丢弃"
        assert resp.json()["path"] == "/x"

    def test_stream_matches_non_stream(self, adapter, http_server: str):
        a = adapter.download(f"{http_server}/x", method="GET", stream=False, kwargs="{}")
        b = adapter.download(f"{http_server}/x", method="GET", stream=True, kwargs="{}")
        # 回显里含随机 User-Agent，只比对稳定字段
        assert a.status_code == b.status_code
        assert (a.json()["method"], a.json()["path"]) == (b.json()["method"], b.json()["path"])
        assert len(b.content) > 0

    def test_user_agent_fallback_without_generator(self, adapter, http_server: str):
        """回归：httpx 适配器 fallback 到不存在的 self.user_agent，必 AttributeError。"""
        adapter.ua_generator = None
        assert adapter._get_user_agent()


class TestProxyParameter:
    """回归：httpx 0.28 移除了 proxies= 参数，旧代码一走代理就 TypeError。"""

    @pytest.mark.skipif(not HTTPX_AVAILABLE, reason="httpx 未安装")
    def test_httpx_client_accepts_proxy(self):
        adapter = HttpxAdapter()
        try:
            client = adapter._get_client("http://127.0.0.1:9", verify=True)
            assert client is not None
        finally:
            adapter.close()

    @pytest.mark.skipif(not CURL_CFFI_AVAILABLE, reason="curl_cffi 未安装")
    def test_curl_cffi_session_accepts_proxy(self):
        adapter = CurlCffiAdapter()
        try:
            session = adapter._get_session("http://127.0.0.1:9", verify=True, impersonate="chrome")
            assert session is not None
        finally:
            adapter.close()

    @pytest.mark.skipif(not CURL_CFFI_AVAILABLE, reason="curl_cffi 未安装")
    def test_curl_cffi_disables_env_proxy_explicitly(self):
        """回归：libcurl 自己读 http_proxy 环境变量，Session(trust_env=False)
        挡不住；proxies=None / {} 也都无效。必须显式传空字符串，否则"不指定代理"
        会静默走服务端机器的环境代理。"""
        adapter = CurlCffiAdapter()
        try:
            assert adapter.trust_env is False
            assert adapter._build_proxies(None) == {"http": "", "https": ""}
            assert adapter._build_proxies("http://p:1") == {"http": "http://p:1", "https": "http://p:1"}

            adapter.trust_env = True
            assert adapter._build_proxies(None) is None
        finally:
            adapter.close()

    @pytest.mark.skipif(not HTTPX_AVAILABLE, reason="httpx 未安装")
    def test_httpx_does_not_trust_env_by_default(self):
        """回归：httpx trust_env 默认 True 会捡起环境里的 ALL_PROXY，
        本机若配了 socks5 还会直接 ImportError。"""
        adapter = HttpxAdapter()
        try:
            assert adapter.trust_env is False
            client = adapter._get_client(None, verify=True)
            assert client.trust_env is False
        finally:
            adapter.close()

    @pytest.mark.skipif(not HTTPX_AVAILABLE, reason="httpx 未安装")
    def test_separate_clients_per_proxy(self):
        adapter = HttpxAdapter()
        try:
            adapter._get_client(None, True)
            adapter._get_client("http://127.0.0.1:9", True)
            assert len(adapter._clients) == 2
        finally:
            adapter.close()


class TestUnsupportedMethod:
    @pytest.mark.skipif(not CURL_CFFI_AVAILABLE, reason="curl_cffi 未安装")
    def test_unknown_method_raises(self):
        adapter = CurlCffiAdapter()
        try:
            resp = adapter.download("http://127.0.0.1/", method="BREW", max_retries=0, kwargs="{}")
            # retry 装饰器会把异常转成错误响应
            assert resp.status_code == -1
            assert isinstance(resp.exception, AdapterError)
        finally:
            adapter.close()
