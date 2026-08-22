"""逐跳重定向准入的回归测试。

SSRF 准入原本只校验调用方给的那一个 URL，适配器默认让底层库自行跟随重定向、
跟随时不再过策略——一次 ``302 Location: http://169.254.169.254/`` 就能把云元数据
取回来。这里钉住"每跳发出之前都校验"这条不变量。
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
import threading
from typing import Any, final

import pytest
from typing_extensions import override

from ipclick.adapters.redirects import DEFAULT_MAX_REDIRECTS, afollow_with_policy, follow_with_policy
from ipclick.exceptions import URLNotAllowedError, ValidationError


@final
class _Resp:
    def __init__(self, status_code: int, location: str = "") -> None:
        self.status_code: int = status_code
        self.headers: dict[str, str] = {"Location": location} if location else {}


def _recorder() -> tuple[list[tuple[str, str, Any]], Any]:
    calls: list[tuple[str, str, Any]] = []

    def send(url: str, method: str, body: Any) -> Any:
        calls.append((url, method, body))
        return _Resp(200)

    return calls, send


def test_redirect_target_is_validated_before_the_hop_is_sent() -> None:
    """被策略拒绝的重定向目标：请求根本不该发出去。"""
    sent: list[str] = []

    def send(url: str, _method: str, _body: Any) -> Any:
        sent.append(url)
        if url == "http://attacker.example/r":
            return _Resp(302, "http://169.254.169.254/latest/meta-data/")
        return _Resp(200)

    def validate(url: str) -> None:
        if "169.254.169.254" in url:
            raise URLNotAllowedError(f"禁止访问云元数据地址: {url}")

    with pytest.raises(URLNotAllowedError):
        _ = follow_with_policy(send, "http://attacker.example/r", "GET", None, validate)

    # 关键断言：元数据地址一次都没被请求过
    assert sent == ["http://attacker.example/r"]


def test_relative_location_is_resolved_before_validating() -> None:
    """Location 是相对路径时也要解析成绝对地址再校验，否则基于主机名的策略会被绕过。"""
    seen: list[str] = []

    def send(url: str, _method: str, _body: Any) -> Any:
        return _Resp(302, "/latest/meta-data/") if url.endswith("/r") else _Resp(200)

    def validate(url: str) -> None:
        seen.append(url)

    _ = follow_with_policy(send, "http://169.254.169.254/r", "GET", None, validate)
    assert seen == ["http://169.254.169.254/latest/meta-data/"]


@pytest.mark.parametrize(
    ("status", "expected_method"), [(301, "GET"), (302, "GET"), (303, "GET"), (307, "POST"), (308, "POST")]
)
def test_method_rewriting_follows_the_rfc(status: int, expected_method: str) -> None:
    """301/302/303 把 POST 改写成 GET 并丢掉请求体；307/308 保持方法与请求体。"""
    calls: list[tuple[str, str, Any]] = []

    def send(url: str, method: str, body: Any) -> Any:
        calls.append((url, method, body))
        return _Resp(status, "http://example.com/next") if len(calls) == 1 else _Resp(200)

    _ = follow_with_policy(send, "http://example.com/start", "POST", {"data": b"x"}, lambda _u: None)

    assert calls[1][1] == expected_method
    if expected_method == "GET":
        assert calls[1][2] is None
    else:
        assert calls[1][2] == {"data": b"x"}


def test_redirect_chain_is_capped() -> None:
    """无限重定向必须报错而不是转圈。"""

    def send(_url: str, _method: str, _body: Any) -> Any:
        return _Resp(302, "http://example.com/loop")

    with pytest.raises(ValidationError, match="重定向次数超过上限"):
        _ = follow_with_policy(send, "http://example.com/loop", "GET", None, lambda _u: None)


def test_non_redirect_returns_immediately() -> None:
    """非重定向响应原样返回，只发一次请求。"""
    calls, send = _recorder()
    resp = follow_with_policy(send, "http://example.com/", "GET", None, lambda _u: None)
    assert resp.status_code == 200
    assert len(calls) == 1


def test_redirect_without_location_is_returned_as_is() -> None:
    """声明了重定向却没给 Location：没有下一跳，原样返回让调用方看到。"""

    def send(_url: str, _method: str, _body: Any) -> Any:
        return _Resp(302)

    resp = follow_with_policy(send, "http://example.com/", "GET", None, lambda _u: None)
    assert resp.status_code == 302


def test_adapters_skip_manual_following_without_a_validator() -> None:
    """没有注入校验器时（进程内直接用适配器）保持底层库的原有行为。"""
    from ipclick.adapters.curl_cffi_adapter import CurlCffiAdapter

    adapter = CurlCffiAdapter()
    assert adapter.url_validator is None

    captured: dict[str, Any] = {}

    class _Session:
        def request(self, _method: str, _url: str, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return _Resp(200)

    _ = adapter._request_following_policy(_Session(), "GET", "http://example.com/", {"allow_redirects": True}, True)
    # 交给库自己跟随，不改 allow_redirects
    assert captured["allow_redirects"] is True


def test_default_max_redirects_is_sane() -> None:
    assert 5 <= DEFAULT_MAX_REDIRECTS <= 20


@final
class _FakeRequest:
    def __init__(self, url: str, redirected_from: Any = None) -> None:
        self.url: str = url
        self.redirected_from: Any = redirected_from


@final
class _FakeBrowserResponse:
    def __init__(self, url: str, request: Any) -> None:
        self.url: str = url
        self.request: Any = request


def _plan(url: str, validator: Any) -> Any:
    from ipclick.adapters.browser_adapter import _RenderPlan

    return _RenderPlan(
        url=url,
        context_options={},
        cookies=[],
        block_resources=(),
        wait_until="load",
        page_timeout_ms=1000,
        script_timeout=1.0,
        url_validator=validator,
    )


def test_browser_rejects_a_redirect_chain_that_violates_policy() -> None:
    """浏览器路径拦不住请求发出，但必须拒绝把响应体交回调用方。

    Playwright 的 context.route 处理器对重定向目标不会再次触发（重定向由浏览器网络栈
    内部跟随完），所以这里只能事后走重定向链。掐掉正文仍有实际意义——SSRF 读云元数据
    的目的就是拿到那段正文。
    """
    from ipclick.adapters.browser_adapter import _reject_disallowed_redirects

    first = _FakeRequest("http://attacker.example/r")
    second = _FakeRequest("http://169.254.169.254/latest/meta-data/", redirected_from=first)
    response = _FakeBrowserResponse("http://169.254.169.254/latest/meta-data/", second)

    def validate(url: str) -> None:
        if "169.254.169.254" in url:
            raise URLNotAllowedError("禁止访问云元数据地址")

    with pytest.raises(URLNotAllowedError, match="重定向目标被 URL 策略拒绝"):
        _reject_disallowed_redirects(response, _plan("http://attacker.example/r", validate))


def test_browser_allows_a_clean_redirect_chain() -> None:
    """链上每一跳都合规时不得误伤。"""
    from ipclick.adapters.browser_adapter import _reject_disallowed_redirects

    first = _FakeRequest("http://example.com/a")
    second = _FakeRequest("http://example.com/b", redirected_from=first)
    response = _FakeBrowserResponse("http://example.com/b", second)

    _reject_disallowed_redirects(response, _plan("http://example.com/a", lambda _u: None))


def test_browser_skips_the_check_without_a_validator() -> None:
    """没注入校验器时（进程内直接用适配器）不做任何额外校验。"""
    from ipclick.adapters.browser_adapter import _reject_disallowed_redirects

    response = _FakeBrowserResponse("http://169.254.169.254/", _FakeRequest("http://169.254.169.254/"))
    _reject_disallowed_redirects(response, _plan("http://attacker.example/", None))


async def test_async_follow_validates_before_each_hop() -> None:
    """异步跟随循环与同步版共用同一套判定，行为必须一致。"""
    sent: list[str] = []

    async def send(url: str, _method: str, _body: Any) -> Any:
        sent.append(url)
        if url == "http://attacker.example/r":
            return _Resp(302, "http://169.254.169.254/latest/meta-data/")
        return _Resp(200)

    def validate(url: str) -> None:
        if "169.254.169.254" in url:
            raise URLNotAllowedError(f"禁止访问云元数据地址: {url}")

    with pytest.raises(URLNotAllowedError):
        _ = await afollow_with_policy(send, "http://attacker.example/r", "GET", None, validate)

    assert sent == ["http://attacker.example/r"]


async def test_async_follow_enforces_the_hop_cap() -> None:
    """异步版同样受 max_redirects 约束，不能无限跟随。"""
    hops: list[str] = []

    async def send(url: str, _method: str, _body: Any) -> Any:
        hops.append(url)
        return _Resp(302, f"/next{len(hops)}")

    with pytest.raises(ValidationError):
        _ = await afollow_with_policy(send, "http://a.example/0", "GET", None, lambda _u: None)

    assert len(hops) == DEFAULT_MAX_REDIRECTS + 1


# ---------------------------------------------------------------------------
# 适配器接线：逐跳校验必须在**三条**路径上都生效
#
# 实测过的绕过：装了 url_validator 的适配器走 adownload 或 download_stream 时，
# 校验器被调用 0 次，一次 302 就把目标取回来了——也就是说
# [SERVER].async_mode = true 一开、或者任何流式请求，整套逐跳 SSRF 准入都不存在。
# ---------------------------------------------------------------------------


@final
class _StubResp:
    def __init__(self, url: str, status_code: int, location: str = "", body: bytes = b"") -> None:
        self.url: str = url
        self.status_code: int = status_code
        self.headers: dict[str, str] = {"Location": location} if location else {}
        self.content: bytes = body
        self.text: str = body.decode()
        self.closed: bool = False

    def close(self) -> None:
        self.closed = True

    def iter_content(self, chunk_size: int = 1024) -> Any:
        _ = chunk_size
        return iter([self.content] if self.content else [])


class _HopSession:
    """按 URL 表作答的假 session，记录实际发出的每一跳。"""

    def __init__(self, routes: dict[str, _StubResp]) -> None:
        self.routes: dict[str, _StubResp] = routes
        self.sent: list[str] = []
        self.saw_allow_redirects: list[Any] = []

    def _answer(self, url: str, kw: dict[str, Any]) -> _StubResp:
        self.sent.append(url)
        self.saw_allow_redirects.append(kw.get("allow_redirects"))
        return self.routes[url]

    def request(self, _method: str, url: str, **kw: Any) -> _StubResp:
        return self._answer(url, kw)


@final
class _AsyncHopSession(_HopSession):
    @override
    async def request(self, _method: str, url: str, **kw: Any) -> _StubResp:
        return self._answer(url, kw)


@final
class _LeaseCache:
    """只提供 lease() 的假 session 缓存。"""

    def __init__(self, session: Any) -> None:
        self.session: Any = session

    @contextmanager
    def lease(self, _key: object) -> Generator[Any]:
        yield self.session


_ENTRY = "http://target.example/start"
_BLOCKED = "http://169.254.169.254/latest/meta-data/"


def _blocking_validator(seen: list[str]) -> Any:
    def validate(url: str) -> None:
        seen.append(url)
        if "169.254.169.254" in url:
            raise URLNotAllowedError(f"禁止访问云元数据地址: {url}")

    return validate


def _adapter(kind: str, session: Any, seen: list[str], *, is_async: bool) -> Any:
    """装一个只有 url_validator 与假 session 缓存的适配器；不碰网络也不建真 session。"""
    from ipclick.adapters.settings import AdapterSettings

    if kind == "curl_cffi":
        from ipclick.adapters.curl_cffi_adapter import CurlCffiAdapter

        adapter: Any = object.__new__(CurlCffiAdapter)
    else:
        from ipclick.adapters.niquests_adapter import NiquestsAdapter

        adapter = object.__new__(NiquestsAdapter)
    adapter.timeout = 60
    adapter.settings = AdapterSettings()
    # 只补 UA 池那几个字段：niquests 的 _request_kwargs 会取默认 User-Agent
    adapter._ua_pool_cache = ("ipclick-test-agent",)
    adapter._ua_lock = threading.Lock()
    adapter.url_validator = _blocking_validator(seen)
    cache = _LeaseCache(session)
    adapter._async_sessions = cache if is_async else _LeaseCache(None)
    adapter._sessions = _LeaseCache(None) if is_async else cache
    return adapter


ADAPTERS = ("curl_cffi", "niquests")


@pytest.mark.parametrize("kind", ADAPTERS)
async def test_async_download_validates_every_redirect_hop(kind: str) -> None:
    """adownload 必须逐跳校验；被拒的那一跳一个字节都不能发出去。"""
    seen: list[str] = []
    session = _AsyncHopSession(
        {
            _ENTRY: _StubResp(_ENTRY, 302, location=_BLOCKED),
            _BLOCKED: _StubResp(_BLOCKED, 200, body=b"CREDENTIALS"),
        }
    )
    adapter = _adapter(kind, session, seen, is_async=True)

    with pytest.raises(URLNotAllowedError):
        _ = await adapter.adownload(_ENTRY, method="GET", allow_redirects=True)

    assert session.sent == [_ENTRY], "元数据地址一次都不该被请求"
    assert seen == [_BLOCKED]
    # 逐跳跟随的前提：底层库自己的跟随必须关掉
    assert session.saw_allow_redirects == [False]


@pytest.mark.parametrize("kind", ADAPTERS)
async def test_async_download_still_follows_allowed_redirects(kind: str) -> None:
    """修复不能把正常的重定向跟随弄坏。"""
    final = "http://target.example/final"
    session = _AsyncHopSession(
        {
            _ENTRY: _StubResp(_ENTRY, 302, location="/final"),
            final: _StubResp(final, 200, body=b"OK"),
        }
    )
    adapter = _adapter(kind, session, [], is_async=True)

    response = await adapter.adownload(_ENTRY, method="GET", allow_redirects=True)

    assert response.status_code == 200
    assert response.content == b"OK"
    assert session.sent == [_ENTRY, final]


@pytest.mark.parametrize("kind", ADAPTERS)
def test_stream_validates_every_redirect_hop(kind: str) -> None:
    """download_stream 同理；被拒时以错误 header 收场，机密不得回传。"""
    seen: list[str] = []
    session = _HopSession(
        {
            _ENTRY: _StubResp(_ENTRY, 302, location=_BLOCKED),
            _BLOCKED: _StubResp(_BLOCKED, 200, body=b"CREDENTIALS"),
        }
    )
    adapter = _adapter(kind, session, seen, is_async=False)

    events = list(adapter.download_stream(_ENTRY, chunk_size=8, method="GET", allow_redirects=True))

    assert session.sent == [_ENTRY], "元数据地址一次都不该被请求"
    assert b"CREDENTIALS" not in b"".join(e for e in events if isinstance(e, bytes))
    header = events[0]
    assert not isinstance(header, bytes)
    assert header.status_code == -1
    assert "169.254.169.254" in (header.error or "")


@pytest.mark.parametrize("kind", ADAPTERS)
def test_stream_releases_the_intermediate_hop_and_returns_the_last_one(kind: str) -> None:
    """中间跳只用来读 Location，必须当场关掉还连接；返回的是最后一跳那条流。"""
    final = "http://target.example/final"
    hop = _StubResp(_ENTRY, 302, location="/final")
    last = _StubResp(final, 200, body=b"payload")
    adapter = _adapter(kind, _HopSession({_ENTRY: hop, final: last}), [], is_async=False)

    events = list(adapter.download_stream(_ENTRY, chunk_size=1024, method="GET", allow_redirects=True))

    assert b"".join(e for e in events if isinstance(e, bytes)) == b"payload"
    assert hop.closed is True, "中间跳没关，连接会一路占着直到 GC"
    assert last.closed is True, "生成器结束时最终响应也要关"


@final
class _FakeDrissionTab:
    """够 DrissionPageAdapter._render 跑完一次的最小 tab。"""

    def __init__(self, final_url: str) -> None:
        self.url: str = final_url
        self.html: str = "<html>secret</html>"
        self.closed: bool = False
        self.cookies_cleared: bool = False
        tab = self

        @final
        class _Cookies:
            def clear(self) -> None:
                tab.cookies_cleared = True

        @final
        class _Set:
            cookies: _Cookies = _Cookies()

            def headers(self, _headers: dict[str, str]) -> None:
                return None

            def blocked_urls(self, _patterns: list[str]) -> None:
                return None

        @final
        class _Listen:
            def start(self, *, targets: str, method: str) -> None:
                _ = (targets, method)
                return None

            def wait(self, *, timeout: float, raise_err: bool) -> Any:
                # 关键字名由生产代码决定，不能改成 _timeout / _raise_err
                _ = (timeout, raise_err)
                return _FakeDrissionPacket(tab.url)

            def stop(self) -> None:
                return None

        self.set: _Set = _Set()
        self.listen: _Listen = _Listen()

    def get(self, _url: str, *, timeout: float, retry: int) -> bool:
        _ = (timeout, retry)
        return True

    def run_js(self, _script: str) -> Any:
        return None

    def wait(self, _seconds: float) -> None:
        return None

    def close(self) -> None:
        self.closed = True


@final
class _FakeDrissionPacket:
    def __init__(self, url: str) -> None:
        self.url: str = url
        self.response: Any = type("R", (), {"status": 200, "headers": {"content-type": "text/html"}})()


def _drission_adapter(tab: _FakeDrissionTab, validator: Any) -> Any:
    from ipclick.adapters.browser_settings import BrowserSettings
    from ipclick.adapters.drission_adapter import DrissionPageAdapter

    adapter: Any = object.__new__(DrissionPageAdapter)
    adapter.browser_settings = BrowserSettings()
    adapter.url_validator = validator
    adapter._ensure_browser = lambda: type("B", (), {"new_tab": staticmethod(lambda: tab)})()
    return adapter


def test_drissionpage_rejects_a_redirect_that_violates_policy() -> None:
    """DrissionPage 这条路此前一次校验都不做。

    url_validator 在 drission_adapter.py 里根本没被读过——curl_cffi / niquests /
    browser 三条路都逐跳校验，唯独它没有。于是
    ``302 Location: http://169.254.169.254/`` 会被 chromium 跟到底，云元数据连同
    tab.url 一起交回调用方，而 SSRF 准入只看过入口 URL。
    """
    tab = _FakeDrissionTab("http://169.254.169.254/latest/meta-data/")

    def validate(url: str) -> None:
        if "169.254.169.254" in url:
            raise URLNotAllowedError("禁止访问云元数据地址")

    adapter = _drission_adapter(tab, validate)

    with pytest.raises(URLNotAllowedError, match="重定向目标被 URL 策略拒绝"):
        _ = adapter._render("http://attacker.example/r", None, None, {}, None, 5.0)

    # 即便被拒，tab 也要照常关掉
    assert tab.closed is True


def test_drissionpage_allows_a_clean_navigation() -> None:
    """同源、合规的导航不得误伤。"""
    tab = _FakeDrissionTab("http://example.com/a")
    adapter = _drission_adapter(tab, lambda _u: None)

    response = adapter._render("http://example.com/a", None, None, {}, None, 5.0)

    assert response.status_code == 200
    assert response.text == "<html>secret</html>"


def test_drissionpage_skips_the_check_without_a_validator() -> None:
    """没注入校验器时（进程内直接用适配器）不做额外校验。"""
    tab = _FakeDrissionTab("http://169.254.169.254/")
    adapter = _drission_adapter(tab, None)

    assert adapter._render("http://attacker.example/", None, None, {}, None, 5.0).status_code == 200


@pytest.mark.parametrize(
    "method_name",
    ["_hop_sender", "_request_following_policy", "_arequest_following_policy", "_stream_following_policy"],
)
def test_both_http_adapters_share_one_hop_following_implementation(method_name: str) -> None:
    """两个 HTTP 适配器必须用同一份逐跳跟随实现，不能各存一份。

    原来 curl_cffi 与 niquests 各有一份逐字相同的拷贝。那份复制已经付过代价：files 只在
    同步路径上被拦、connect_timeout 只有 niquests 真的用上，两处都是"改了一边忘了另一边"。
    这一层压着 SSRF 准入，一旦重新分叉，就会再次出现"某条路径不校验"的缺口。
    """
    from ipclick.adapters.curl_cffi_adapter import CurlCffiAdapter
    from ipclick.adapters.niquests_adapter import NiquestsAdapter
    from ipclick.adapters.redirects import HopFollowingMixin

    shared = getattr(HopFollowingMixin, method_name)
    for adapter_cls in (CurlCffiAdapter, NiquestsAdapter):
        assert method_name not in vars(adapter_cls), f"{adapter_cls.__name__} 又自己实现了 {method_name}"
        assert getattr(adapter_cls, method_name) is shared
