"""浏览器渲染适配器（Playwright 系：playwright / patchright / camoufox）。

分两层：

* 参数校验、注册、代理组装——不需要浏览器，任何环境都跑。
* 真实渲染——按引擎参数化，装了哪个跑哪个，都没装就整组 skip。

CI 会装 chromium，所以 playwright / patchright 那两轮在 CI 上是真的跑起来的
（见 .github/workflows/ci.yml）。DrissionPage 在 test_drission_adapter.py。
"""

from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import threading
from typing import ClassVar

import pytest

from ipclick.adapters import browser_engines, registry
from ipclick.adapters.browser_adapter import (
    ENGINE_ADAPTERS,
    BrowserAdapter,
    PlaywrightAdapter,
    _normalize_cookies,
)
from ipclick.adapters.browser_settings import BrowserSettings
from ipclick.exceptions import AdapterError, ValidationError
from ipclick.utils.url_util import merge_query_params


pytestmark = pytest.mark.skipif(not browser_engines.available_engines(), reason="没有任何浏览器引擎")

#: 系统里常见的 chromium 位置。找不到就退回 playwright 自己下载的那份。
_SYSTEM_BROWSERS = ("/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome")

PAGE = b"""<!doctype html><html><head><title>t</title></head><body>
<div id="app">LOADING</div>
<img src="/pixel.png">
<script>setTimeout(function(){document.getElementById('app').textContent='RENDERED-BY-JS'},150)</script>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    #: 收到的请求，供断言使用
    seen: ClassVar[list[tuple[str, dict[str, str]]]] = []

    def log_message(self, *args: object) -> None:
        pass

    def _send(self, status: int, body: bytes, content_type: str = "text/html") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # 逐个关闭连接。Firefox（camoufox）会把 keep-alive 连接挂很久，
        # 单线程的 HTTPServer 会因此收不到下一个请求而超时——那是测试用服务器
        # 的毛病，不是适配器的。这里连同 ThreadingHTTPServer 一起把变量消掉。
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_GET(self) -> None:
        type(self).seen.append((self.path, dict(self.headers)))
        if self.path.startswith("/missing"):
            self._send(404, b"<h1>nope</h1>")
        elif self.path.startswith("/pixel.png"):
            self._send(200, b"\x89PNG\r\n\x1a\n", "image/png")
        elif self.path.startswith("/echo"):
            payload = json.dumps({"path": self.path, "headers": dict(self.headers)}).encode()
            # 刻意用 text/plain 而不是 application/json：Firefox（camoufox）会用
            # 内置的 JSON 查看器渲染 application/json，那是个很重的 devtools 页面，
            # 会让用例慢到不可用。这些用例断言的是"服务端收到了什么"，
            # 浏览器怎么渲染无关紧要。
            self._send(200, payload, "text/plain")
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


def _settings(engine: str = "playwright", **overrides: object) -> BrowserSettings:
    executable = next((p for p in _SYSTEM_BROWSERS if Path(p).exists()), None)
    base: dict[str, object] = {
        "engine": engine,
        # camoufox 自带 Firefox，不能把系统 chromium 塞给它
        "executable_path": None if engine == "camoufox" else executable,
        # 容器 / CI 里通常没有 user namespace，不关沙箱起不来
        "no_sandbox": True,
        # 页面里那个 <img> 默认会被拦掉，某些用例需要放行
        "block_resources": (),
        # camoufox 是 Firefox + 一堆扩展，单个 context 的内存开销远大于
        # Chromium 系。小内存机器上开 4 个并发 context 会直接开始换页，
        # 请求从几秒变成几分钟。测试里按引擎给不同的并发额度。
        "max_pages": 2 if engine == "camoufox" else 4,
    }
    base.update(overrides)
    return BrowserSettings(**base)  # pyright: ignore[reportArgumentType]


#: 参与渲染用例参数化的引擎。装了哪个就测哪个，一个都没有时整组 skip。
#:
#: camoufox 默认**不在**列表里：它是 Firefox 加一整套扩展，单个 context 的内存
#: 开销远大于 Chromium 系。在内存紧张的机器上（实测 4GB、可用 200MB）跑完整套
#: 渲染用例会开始换页，请求从几秒变成几分钟，看起来像卡死。功能本身是好的——
#: 顺序请求、并发、截图、脚本、cookie 隔离都验过。
#:
#: 内存够的机器上加进来：
#:     IPCLICK_TEST_ENGINES=playwright,patchright,camoufox uv run pytest
_RENDER_ENGINES = [
    e.strip() for e in os.environ.get("IPCLICK_TEST_ENGINES", "playwright,patchright").split(",") if e.strip()
]


@pytest.fixture(scope="module", params=_RENDER_ENGINES)
def browser(request: pytest.FixtureRequest) -> Iterator[BrowserAdapter]:
    """每个引擎一个模块级适配器。

    三个引擎共用同一套渲染代码，所以它们必须表现一致——这正是参数化的意义：
    camoufox 换了 Firefox 内核，很多 Chromium 下想当然的行为在它那里不成立。
    """
    engine = str(request.param)
    if not browser_engines.is_available(engine):
        pytest.skip(f"{engine} 未安装")

    adapter = ENGINE_ADAPTERS[engine](browser_settings=_settings(engine, allow_scripts=True))
    try:
        # 先探一次：浏览器装不上就整组 skip，而不是让每个用例各报一次错
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            dead = int(s.getsockname()[1])
        probe = adapter.download(f"http://127.0.0.1:{dead}/", method="GET", max_retries=0, kwargs="{}")
        if isinstance(probe.exception, AdapterError) and "启动失败" in str(probe.exception):
            pytest.skip(f"{engine} 没有可用的浏览器: {probe.exception}")
        yield adapter
    finally:
        adapter.close()


# ---------------------------------------------------------------------- #
# 不需要浏览器
# ---------------------------------------------------------------------- #


class TestRegistration:
    def test_registered_when_installed(self):
        assert registry.ADAPTER_CLASSES.get("playwright") is PlaywrightAdapter

    def test_hint_mentions_browser_download(self, monkeypatch: pytest.MonkeyPatch):
        """光 pip install 还不够，还得下浏览器——报错里不说清楚，人一定会卡住。"""
        monkeypatch.delitem(registry.ADAPTER_CLASSES, "playwright", raising=False)
        with pytest.raises(AdapterError, match="playwright install chromium"):
            registry.get_adapter("playwright")

    def test_registry_passes_browser_settings(self):
        """回归：get_adapter 只传 AdapterSettings 的话，[BROWSER] 又变成死配置。"""
        adapter = registry.get_adapter("playwright", None, _settings("playwright", max_pages=9))
        try:
            assert isinstance(adapter, PlaywrightAdapter)
            assert adapter.browser_settings.max_pages == 9
        finally:
            adapter.close()

    def test_disabled_by_config(self):
        with pytest.raises(AdapterError, match="enabled = false"):
            PlaywrightAdapter(browser_settings=_settings("playwright", enabled=False))


class TestParameterValidation:
    """这些都在启动浏览器之前就该失败——不该白白拉起一个浏览器进程。"""

    @pytest.fixture
    def adapter(self) -> Iterator[PlaywrightAdapter]:
        instance = PlaywrightAdapter(browser_settings=_settings())
        try:
            yield instance
        finally:
            instance.close()

    def test_non_get_rejected(self, adapter: PlaywrightAdapter):
        with pytest.raises(ValidationError, match="只支持 GET"):
            adapter.download("http://example.com", method="POST", kwargs="{}")

    def test_body_rejected(self, adapter: PlaywrightAdapter):
        with pytest.raises(ValidationError, match="不能携带请求体"):
            adapter.download("http://example.com", method="GET", json={"a": 1}, kwargs="{}")

    def test_disabling_redirects_rejected(self, adapter: PlaywrightAdapter):
        """浏览器总是跟随重定向。默默忽略这个参数就是静默给出错误结果。"""
        with pytest.raises(ValidationError, match="无法禁用重定向"):
            adapter.download("http://example.com", method="GET", allow_redirects=False, kwargs="{}")

    def test_malformed_automation_config(self, adapter: PlaywrightAdapter):
        with pytest.raises(ValidationError, match="不是合法的 JSON"):
            adapter.download("http://example.com", method="GET", automation_config="{oops", kwargs="{}")

    def test_automation_config_must_be_object(self, adapter: PlaywrightAdapter):
        with pytest.raises(ValidationError, match="必须是 JSON 对象"):
            adapter.download("http://example.com", method="GET", automation_config="[1,2]", kwargs="{}")

    def test_unknown_wait_until(self, adapter: PlaywrightAdapter):
        with pytest.raises(ValidationError, match="wait_until"):
            adapter.download(
                "http://example.com", method="GET", automation_config='{"wait_until":"whenever"}', kwargs="{}"
            )

    def test_unknown_block_resource(self, adapter: PlaywrightAdapter):
        with pytest.raises(ValidationError, match="未知资源类型"):
            adapter.download(
                "http://example.com", method="GET", automation_config='{"block_resources":["hologram"]}', kwargs="{}"
            )

    def test_script_rejected_when_not_allowed(self, adapter: PlaywrightAdapter):
        """默认不许在页面里跑 JS：那等于绕开服务端的 URL 安全策略。"""
        with pytest.raises(ValidationError, match="allow_scripts"):
            adapter.download("http://example.com", method="GET", automation_script="() => 1", kwargs="{}")

    def test_validation_errors_are_not_retried(self, adapter: PlaywrightAdapter):
        """回归：retry 装饰器原先会把参数错误吞成 -1 响应，还先睡满 1+2+4 秒。"""
        import time

        start = time.monotonic()
        with pytest.raises(ValidationError):
            adapter.download("http://example.com", method="POST", max_retries=3, retry_delay=1.0, kwargs="{}")
        assert time.monotonic() - start < 1.0, "参数错误不该走退避重试"


class TestPlanBuilding:
    """代理、cookie、params 的组装——不用真起浏览器就能验。"""

    @pytest.fixture
    def adapter(self) -> Iterator[PlaywrightAdapter]:
        instance = PlaywrightAdapter(
            browser_settings=_settings("playwright", proxy_gateway="http://cfg:1", allow_scripts=True)
        )
        try:
            yield instance
        finally:
            instance.close()

    def _plan(self, adapter: PlaywrightAdapter, **overrides: object):
        kwargs: dict[str, object] = {
            "headers": None,
            "cookies": None,
            "params": None,
            "proxy": None,
            "timeout": 30,
            "verify": True,
            "automation_config": None,
            "automation_script": None,
        }
        kwargs.update(overrides)
        return adapter._build_plan("http://example.com/p", **kwargs)  # pyright: ignore[reportArgumentType]

    def test_config_proxy_used_by_default(self, adapter: PlaywrightAdapter):
        assert self._plan(adapter).context_options["proxy"] == {"server": "http://cfg:1"}

    def test_request_proxy_overrides_config(self, adapter: PlaywrightAdapter):
        plan = self._plan(adapter, proxy="http://req:2")
        assert plan.context_options["proxy"]["server"] == "http://req:2"

    def test_no_proxy_key_when_none_configured(self):
        """没配代理时必须完全不传 proxy 参数。传个占位值会让所有直连请求
        变成 ERR_PROXY_CONNECTION_FAILED。"""
        adapter = PlaywrightAdapter(browser_settings=_settings("playwright"))
        try:
            plan = self._plan(adapter)
            assert "proxy" not in plan.context_options
        finally:
            adapter.close()

    def test_verify_false_ignores_https_errors(self, adapter: PlaywrightAdapter):
        assert self._plan(adapter, verify=False).context_options["ignore_https_errors"] is True
        assert self._plan(adapter, verify=True).context_options["ignore_https_errors"] is False

    def test_timeout_converted_to_ms(self, adapter: PlaywrightAdapter):
        assert self._plan(adapter, timeout=12).page_timeout_ms == 12000

    def test_zero_timeout_uses_config(self, adapter: PlaywrightAdapter):
        assert self._plan(adapter, timeout=0).page_timeout_ms == 30000

    def test_headers_passed_through(self, adapter: PlaywrightAdapter):
        plan = self._plan(adapter, headers={"X-Demo": "1"})
        assert plan.context_options["extra_http_headers"] == {"X-Demo": "1"}


class TestHelpers:
    def test_params_appended_to_empty_query(self):
        assert merge_query_params("http://h/p", {"a": "1"}) == "http://h/p?a=1"

    def test_params_merged_with_existing_query(self):
        """直接覆盖 query 会把 URL 里原有的参数弄丢。"""
        assert merge_query_params("http://h/p?x=9", {"a": "1"}) == "http://h/p?x=9&a=1"

    def test_params_none_is_noop(self):
        assert merge_query_params("http://h/p?x=9", None) == "http://h/p?x=9"

    def test_cookie_dict(self):
        assert _normalize_cookies({"a": "1"}, "http://h/") == [{"name": "a", "value": "1", "url": "http://h/"}]

    def test_cookie_header_string(self):
        got = _normalize_cookies("a=1; b=2", "http://h/")
        assert [(c["name"], c["value"]) for c in got] == [("a", "1"), ("b", "2")]

    def test_cookie_string_ignores_junk(self):
        assert _normalize_cookies("; =nope; a=1", "http://h/") == [{"name": "a", "value": "1", "url": "http://h/"}]


# ---------------------------------------------------------------------- #
# 需要真浏览器
# ---------------------------------------------------------------------- #


class TestRendering:
    def test_javascript_actually_runs(self, browser: BrowserAdapter, http_server: str):
        """整个适配器存在的理由：HTTP 适配器拿到的 HTML 里只有 LOADING。"""
        resp = browser.download(
            f"{http_server}/",
            method="GET",
            automation_config='{"wait_for_selector":"#app","wait_for_timeout":400}',
            kwargs="{}",
        )
        assert resp.status_code == 200
        assert "RENDERED-BY-JS" in resp.text
        assert "LOADING" not in resp.text

    def test_returns_status_code(self, browser: BrowserAdapter, http_server: str):
        resp = browser.download(f"{http_server}/missing", method="GET", max_retries=0, kwargs="{}")
        assert resp.status_code == 404
        assert resp.exception is None

    def test_content_and_text_agree(self, browser: BrowserAdapter, http_server: str):
        resp = browser.download(f"{http_server}/", method="GET", kwargs="{}")
        assert resp.content == resp.text.encode()

    def test_params_reach_the_server(self, browser: BrowserAdapter, http_server: str):
        """浏览器导航没有单独的 params 参数，得自己并进 URL 的 query。"""
        _Handler.seen.clear()
        resp = browser.download(f"{http_server}/echo", method="GET", params={"a": "1"}, kwargs="{}")
        assert "a=1" in resp.url
        assert any(path == "/echo?a=1" for path, _ in _Handler.seen)

    def test_custom_headers_sent(self, browser: BrowserAdapter, http_server: str):
        _Handler.seen.clear()
        browser.download(f"{http_server}/echo", method="GET", headers={"X-Demo": "ipclick"}, kwargs="{}")
        assert any(h.get("X-Demo") == "ipclick" for _, h in _Handler.seen)

    def test_cookies_sent(self, browser: BrowserAdapter, http_server: str):
        _Handler.seen.clear()
        browser.download(f"{http_server}/echo", method="GET", cookies={"sid": "abc"}, kwargs="{}")
        assert any("sid=abc" in h.get("Cookie", "") for _, h in _Handler.seen)

    def test_script_result_returned_in_header(self, browser: BrowserAdapter, http_server: str):
        resp = browser.download(f"{http_server}/", method="GET", automation_script="() => document.title", kwargs="{}")
        assert json.loads(resp.headers["x-ipclick-script-result"]) == "t"

    def test_screenshot_returns_png(self, browser: BrowserAdapter, http_server: str):
        resp = browser.download(f"{http_server}/", method="GET", automation_config='{"screenshot":true}', kwargs="{}")
        assert resp.content.startswith(b"\x89PNG")
        assert resp.headers["content-type"] == "image/png"

    def test_blocked_resources_are_not_fetched(self, browser: BrowserAdapter, http_server: str):
        """拦图片是最主要的省流量手段，得确认请求真的没发出去。"""
        _Handler.seen.clear()
        browser.download(
            f"{http_server}/", method="GET", automation_config='{"block_resources":["image"]}', kwargs="{}"
        )
        assert not any(path.startswith("/pixel.png") for path, _ in _Handler.seen)

    def test_unblocked_resources_are_fetched(self, browser: BrowserAdapter, http_server: str):
        _Handler.seen.clear()
        browser.download(f"{http_server}/", method="GET", automation_config='{"block_resources":[]}', kwargs="{}")
        assert any(path.startswith("/pixel.png") for path, _ in _Handler.seen)

    def test_unreachable_host_becomes_error_response(self, browser: BrowserAdapter):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            dead = int(s.getsockname()[1])
        resp = browser.download(f"http://127.0.0.1:{dead}/", method="GET", max_retries=0, kwargs="{}")
        assert resp.status_code == -1
        assert resp.exception is not None

    def test_explicit_proxy_is_honoured(self, http_server: str):
        """回归：启动时设了 proxy={"server":"per-context"} 会让 context 级代理
        形同虚设——所有请求都去连那个不存在的代理。反过来，如果代理没生效，
        这里指定一个不可达代理却能拿到 200。"""
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            dead_proxy = int(s.getsockname()[1])

        adapter = PlaywrightAdapter(browser_settings=_settings("playwright"))
        try:
            resp = adapter.download(
                f"{http_server}/", method="GET", proxy=f"http://127.0.0.1:{dead_proxy}", max_retries=0, kwargs="{}"
            )
            assert resp.status_code == -1, "指定了不可达代理却请求成功，说明代理没生效"
        finally:
            adapter.close()

    def test_direct_connection_works_without_proxy(self, browser: BrowserAdapter, http_server: str):
        """回归：上面那个 per-context 占位值会让所有直连请求失败。"""
        assert browser.download(f"{http_server}/", method="GET", kwargs="{}").status_code == 200

    def test_concurrent_requests(self, browser: BrowserAdapter, http_server: str):
        """gRPC 请求来自线程池的任意线程，而 playwright 对象绑定事件循环线程。

        并发度取 ``max_pages``（不超额），这样测的是"多线程调用安全"这件事本身。
        超过 max_pages 的排队行为由 test_queues_beyond_max_pages 单独覆盖。
        """
        from concurrent.futures import ThreadPoolExecutor

        count = browser.browser_settings.max_pages
        with ThreadPoolExecutor(count) as pool:
            results = list(
                pool.map(lambda i: browser.download(f"{http_server}/?i={i}", method="GET", kwargs="{}"), range(count))
            )
        assert [r.status_code for r in results] == [200] * count

    def test_queues_beyond_max_pages(self, browser: BrowserAdapter, http_server: str):
        """超出 max_pages 的请求应该排队而不是失败。

        camoufox（Firefox 内核）在这里明显比 Chromium 系吃力：同一时刻创建的
        context 越多，后续操作被拖慢得越厉害。这也是 [BROWSER].max_pages 对它
        要设得更保守的原因，README 已写明。
        """
        from concurrent.futures import ThreadPoolExecutor

        count = browser.browser_settings.max_pages + 2
        with ThreadPoolExecutor(count) as pool:
            results = list(
                pool.map(lambda i: browser.download(f"{http_server}/?i={i}", method="GET", kwargs="{}"), range(count))
            )
        assert [r.status_code for r in results] == [200] * count

    def test_contexts_are_isolated(self, browser: BrowserAdapter, http_server: str):
        """共用 context 会把上一个调用方的 cookie 泄漏给下一个。"""
        browser.download(f"{http_server}/echo", method="GET", cookies={"sid": "secret"}, kwargs="{}")
        _Handler.seen.clear()
        browser.download(f"{http_server}/echo", method="GET", kwargs="{}")
        assert not any("secret" in h.get("Cookie", "") for _, h in _Handler.seen)

    def test_close_is_idempotent(self):
        adapter = PlaywrightAdapter(browser_settings=_settings("playwright"))
        adapter.close()
        adapter.close()

    def test_close_without_any_request(self):
        """浏览器是懒启动的，没发过请求就 close() 不该炸。"""
        PlaywrightAdapter(browser_settings=_settings()).close()
