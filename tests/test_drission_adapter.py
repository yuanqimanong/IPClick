"""DrissionPage 适配器（Windows 上的默认引擎）。

它和 Playwright 系走的是完全不同的 API，所以不能靠 test_browser_adapter.py 的
参数化覆盖——但**对外契约必须一致**，这里就是在验这件事。

参数校验部分不需要浏览器，任何环境都跑；渲染部分需要本机有 Chrome/Chromium。
"""

from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import threading
from typing import ClassVar

import pytest

from ipclick.adapters.browser_settings import BrowserSettings
from ipclick.adapters.drission_adapter import DRISSIONPAGE_AVAILABLE, DrissionPageAdapter, _blocked_patterns
from ipclick.exceptions import AdapterError, ValidationError


pytestmark = pytest.mark.skipif(not DRISSIONPAGE_AVAILABLE, reason="DrissionPage 未安装")

_SYSTEM_BROWSERS = ("/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome")

PAGE = b"""<!doctype html><html><head><title>t</title></head><body>
<div id="app">LOADING</div>
<img src="/pixel.png">
<script>setTimeout(function(){document.getElementById('app').textContent='RENDERED-BY-JS'},150)</script>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: ClassVar[list[str]] = []

    def log_message(self, *args: object) -> None:
        pass

    def _send(self, status: int, body: bytes, content_type: str = "text/html") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # 逐个关闭连接，别让 keep-alive 把测试服务器的连接槽占住
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_GET(self) -> None:
        type(self).seen.append(self.path)
        if self.path.startswith("/missing"):
            self._send(404, b"<h1>nope</h1>")
        elif self.path.startswith("/pixel.png"):
            self._send(200, b"\x89PNG\r\n\x1a\n", "image/png")
        else:
            self._send(200, PAGE)


@pytest.fixture(scope="module")
def http_server() -> Iterator[str]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])

    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _settings(**overrides: object) -> BrowserSettings:
    base: dict[str, object] = {
        "engine": "drissionpage",
        "executable_path": next((p for p in _SYSTEM_BROWSERS if Path(p).exists()), None),
        "no_sandbox": True,
        "block_resources": (),
    }
    base.update(overrides)
    return BrowserSettings(**base)  # pyright: ignore[reportArgumentType]


@pytest.fixture(scope="module")
def browser() -> Iterator[DrissionPageAdapter]:
    adapter = DrissionPageAdapter(browser_settings=_settings(allow_scripts=True))
    try:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            dead = int(s.getsockname()[1])
        probe = adapter.download(f"http://127.0.0.1:{dead}/", method="GET", max_retries=0, kwargs="{}")
        if isinstance(probe.exception, AdapterError) and "启动失败" in str(probe.exception):
            pytest.skip(f"没有可用的 Chrome/Chromium: {probe.exception}")
        yield adapter
    finally:
        adapter.close()


# ---------------------------------------------------------------------- #
# 不需要浏览器
# ---------------------------------------------------------------------- #


class TestParameterValidation:
    """契约必须与 Playwright 系一致——同一个错误用法，换个引擎不该有不同结果。"""

    @pytest.fixture
    def adapter(self) -> Iterator[DrissionPageAdapter]:
        instance = DrissionPageAdapter(browser_settings=_settings())
        try:
            yield instance
        finally:
            instance.close()

    def test_non_get_rejected(self, adapter: DrissionPageAdapter):
        with pytest.raises(ValidationError, match="只支持 GET"):
            adapter.download("http://example.com", method="POST", kwargs="{}")

    def test_body_rejected(self, adapter: DrissionPageAdapter):
        with pytest.raises(ValidationError, match="不能携带请求体"):
            adapter.download("http://example.com", method="GET", json={"a": 1}, kwargs="{}")

    def test_disabling_redirects_rejected(self, adapter: DrissionPageAdapter):
        with pytest.raises(ValidationError, match="无法禁用重定向"):
            adapter.download("http://example.com", method="GET", allow_redirects=False, kwargs="{}")

    def test_malformed_automation_config(self, adapter: DrissionPageAdapter):
        with pytest.raises(ValidationError, match="不是合法的 JSON"):
            adapter.download("http://example.com", method="GET", automation_config="{oops", kwargs="{}")

    def test_script_rejected_when_not_allowed(self, adapter: DrissionPageAdapter):
        with pytest.raises(ValidationError, match="allow_scripts"):
            adapter.download("http://example.com", method="GET", automation_script="return 1", kwargs="{}")

    def test_per_request_proxy_rejected(self, adapter: DrissionPageAdapter):
        """DrissionPage 的代理是浏览器进程级的，启动后改不了。

        默默忽略等于让请求从错误的出口 IP 发出去——对一个代理服务来说，
        这比直接报错严重得多。
        """
        with pytest.raises(ValidationError, match="不支持按请求指定"):
            adapter.download("http://example.com", method="GET", proxy="http://127.0.0.1:9", kwargs="{}")

    def test_configured_proxy_is_not_rejected(self):
        """请求里传的代理与配置一致时不该报错——SDK 会把配置代理原样传下来。"""
        adapter = DrissionPageAdapter(browser_settings=_settings(proxy_gateway="http://p:1"))
        try:
            with pytest.raises(ValidationError, match="只支持 GET"):
                adapter.download("http://example.com", method="POST", proxy="http://p:1", kwargs="{}")
        finally:
            adapter.close()

    def test_validation_errors_are_not_retried(self, adapter: DrissionPageAdapter):
        import time

        start = time.monotonic()
        with pytest.raises(ValidationError):
            adapter.download("http://example.com", method="POST", max_retries=3, retry_delay=1.0, kwargs="{}")
        assert time.monotonic() - start < 1.0

    def test_disabled_by_config(self):
        with pytest.raises(AdapterError, match="enabled = false"):
            DrissionPageAdapter(browser_settings=_settings(enabled=False))


class TestResourceBlocking:
    def test_patterns_cover_common_suffixes(self):
        patterns = _blocked_patterns(["image", "font"])
        assert "*.png" in patterns
        assert "*.woff2" in patterns

    def test_unknown_type_is_ignored(self):
        assert _blocked_patterns(["hologram"]) == []

    def test_empty_means_nothing_blocked(self):
        assert _blocked_patterns([]) == []


# ---------------------------------------------------------------------- #
# 需要真浏览器
# ---------------------------------------------------------------------- #


class TestRendering:
    def test_javascript_actually_runs(self, browser: DrissionPageAdapter, http_server: str):
        resp = browser.download(
            f"{http_server}/",
            method="GET",
            automation_config='{"wait_for_selector":"#app","wait_for_timeout":500}',
            max_retries=0,
            kwargs="{}",
        )
        assert resp.status_code == 200
        assert "RENDERED-BY-JS" in resp.text

    def test_status_code_from_packet(self, browser: DrissionPageAdapter, http_server: str):
        """tab.get() 只返回 bool，状态码得靠 tab.listen 抓包拿。"""
        resp = browser.download(f"{http_server}/missing", method="GET", max_retries=0, kwargs="{}")
        assert resp.status_code == 404

    def test_response_headers_present(self, browser: DrissionPageAdapter, http_server: str):
        resp = browser.download(f"{http_server}/", method="GET", max_retries=0, kwargs="{}")
        assert any(k.lower() == "content-type" for k in resp.headers)

    def test_script_result_in_header(self, browser: DrissionPageAdapter, http_server: str):
        resp = browser.download(
            f"{http_server}/", method="GET", automation_script="return document.title", max_retries=0, kwargs="{}"
        )
        assert json.loads(resp.headers["x-ipclick-script-result"]) == "t"

    def test_screenshot_returns_png(self, browser: DrissionPageAdapter, http_server: str):
        resp = browser.download(
            f"{http_server}/", method="GET", automation_config='{"screenshot":true}', max_retries=0, kwargs="{}"
        )
        assert resp.content.startswith(b"\x89PNG")
        assert resp.headers["content-type"] == "image/png"

    def test_params_merged_into_url(self, browser: DrissionPageAdapter, http_server: str):
        _Handler.seen.clear()
        browser.download(f"{http_server}/echo", method="GET", params={"a": "1"}, max_retries=0, kwargs="{}")
        assert any(p == "/echo?a=1" for p in _Handler.seen)

    def test_unreachable_host_becomes_error_response(self, browser: DrissionPageAdapter):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            dead = int(s.getsockname()[1])
        resp = browser.download(f"http://127.0.0.1:{dead}/", method="GET", max_retries=0, kwargs="{}")
        assert resp.status_code == -1

    def test_sequential_requests_reuse_browser(self, browser: DrissionPageAdapter, http_server: str):
        """浏览器只启动一次，后续请求各开各的 tab。"""
        first = browser.download(f"{http_server}/", method="GET", max_retries=0, kwargs="{}")
        second = browser.download(f"{http_server}/", method="GET", max_retries=0, kwargs="{}")
        assert (first.status_code, second.status_code) == (200, 200)

    def test_close_is_idempotent(self):
        adapter = DrissionPageAdapter(browser_settings=_settings())
        adapter.close()
        adapter.close()

    def test_close_without_any_request(self):
        """浏览器是懒启动的，没发过请求就 close() 不该炸。"""
        DrissionPageAdapter(browser_settings=_settings()).close()
