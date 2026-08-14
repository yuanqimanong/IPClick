"""适配器测试：对本机起的一个小 HTTP 服务发真实请求（不出网）。"""

from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import socket
import threading
import time

import pytest

from ipclick.adapters.curl_cffi_adapter import CURL_CFFI_AVAILABLE, CurlCffiAdapter
from ipclick.adapters.niquests_adapter import NIQUESTS_AVAILABLE, NiquestsAdapter
from ipclick.exceptions import ValidationError


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
    """取适配器的连接池字典（curl_cffi 叫 _sessions，niquests 叫 _sessions）。

    注意不能写成 ``_sessions or _clients``——池为空时是 falsy，会误取到另一个。
    """
    pool = getattr(adapter, "_sessions", None)
    return pool if pool is not None else adapter._clients  # type: ignore[attr-defined]


_ADAPTERS = {
    "curl_cffi": (CURL_CFFI_AVAILABLE, CurlCffiAdapter),
    "niquests": (NIQUESTS_AVAILABLE, NiquestsAdapter),
}


@pytest.fixture(params=sorted(_ADAPTERS))
def adapter(request: pytest.FixtureRequest) -> Iterator[object]:
    available, cls = _ADAPTERS[request.param]
    if not available:
        pytest.skip(f"{request.param} 未安装")
    instance = cls()
    try:
        yield instance
    finally:
        instance.close()


class TestAdapterBehaviour:
    """所有适配器必须表现一致——历史上 httpx 适配器会静默丢掉一堆参数
    （json 体、allow_redirects），这组参数化用例就是为了让这种事再也发生不了。
    httpx 适配器已在 0.3.0 移除，但这些一致性断言对留下的适配器同样有效。"""

    def test_basic_get(self, adapter, http_server: str):
        resp = adapter.download(f"{http_server}/hello", method="GET", kwargs="{}")
        assert resp.status_code == 200
        assert resp.json()["method"] == "GET"

    def test_json_body_is_sent(self, adapter, http_server: str):
        """回归：曾有适配器完全忽略 json 参数，请求体直接丢失。"""
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
        """回归：曾有适配器忽略 allow_redirects，永远跟随重定向。"""
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
        """回归：曾有适配器 fallback 到不存在的 self.user_agent，必 AttributeError。"""
        adapter.ua_generator = None
        assert adapter._get_user_agent()


class TestProxyParameter:
    """代理参数要真能传到底层客户端上。"""

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

    @pytest.mark.skipif(not CURL_CFFI_AVAILABLE, reason="curl_cffi 未安装")
    def test_explicit_proxy_overrides_ambient_no_proxy(self, monkeypatch: pytest.MonkeyPatch, http_server: str):
        """回归：libcurl 自行读取环境里的 no_proxy/NO_PROXY，命中的目标会绕过
        我们设置的代理直连并返回 200——显式指定的代理被静默丢弃。

        这里把本地目标加进 NO_PROXY，再指定一个不可达的代理：修复后请求必须
        走代理（因而失败），而不是无视代理直连成功。
        """
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

        adapter = CurlCffiAdapter()
        try:
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                dead_proxy_port = int(s.getsockname()[1])

            resp = adapter.download(
                f"{http_server}/x",
                method="GET",
                proxy=f"http://127.0.0.1:{dead_proxy_port}",
                max_retries=0,
                kwargs="{}",
            )
            assert resp.status_code == -1, "指定了不可达代理却请求成功，说明代理被 NO_PROXY 绕过了"
        finally:
            adapter.close()

    @pytest.mark.skipif(not CURL_CFFI_AVAILABLE, reason="curl_cffi 未安装")
    def test_no_proxy_option_only_set_when_proxy_given(self):
        """不指定代理时不应干预 no-proxy 行为。"""
        adapter = CurlCffiAdapter()
        try:
            assert adapter._get_session(None, True, "chrome") is not None
            assert adapter._get_session("http://127.0.0.1:9", True, "chrome") is not None
            assert len(adapter._sessions) == 2
        finally:
            adapter.close()


class TestUnsupportedMethod:
    @pytest.mark.skipif(not CURL_CFFI_AVAILABLE, reason="curl_cffi 未安装")
    def test_unknown_method_raises(self):
        """参数错误直接抛，不再被 retry 装饰器吞成 -1 响应。

        伪装成网络失败会误导调用方去查网络，TaskService 那边也就没机会把它
        映射成 INVALID_ARGUMENT。
        """
        adapter = CurlCffiAdapter()
        try:
            with pytest.raises(ValidationError, match="BREW"):
                adapter.download("http://127.0.0.1/", method="BREW", max_retries=0, kwargs="{}")
        finally:
            adapter.close()

    @pytest.mark.skipif(not CURL_CFFI_AVAILABLE, reason="curl_cffi 未安装")
    def test_unknown_method_is_not_retried(self):
        """回归：原先默认配置下要先睡满 1+2+4 秒才返回，而重试永远不可能成功。"""
        adapter = CurlCffiAdapter()
        try:
            start = time.monotonic()
            with pytest.raises(ValidationError):
                adapter.download("http://127.0.0.1/", method="BREW", max_retries=3, retry_delay=1.0, kwargs="{}")
            assert time.monotonic() - start < 1.0
        finally:
            adapter.close()
