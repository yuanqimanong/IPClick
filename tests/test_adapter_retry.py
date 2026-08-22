from __future__ import annotations

from collections.abc import Iterable
from typing import Any, final

import pytest

from ipclick.adapters import retry as retry_module
from ipclick.adapters.base import mark_utf8_charset, normalize_js
from ipclick.adapters.retry import RetryLoop, RetryPolicy, aretry, caller_alive_check, caller_gone, retry
from ipclick.adapters.settings import HARD_MAX_RETRIES, AdapterSettings
from ipclick.dto.response import Response
from ipclick.exceptions import AdapterError, TransportError, ValidationError
from ipclick.trace import get_recorder

from .helpers import FakeAsyncClock, FakeClock


Outcome = int | Exception


class FakeAdapter:
    adapter_name: str = "fake"

    def __init__(self, outcomes: Iterable[Outcome], settings: AdapterSettings | None = None) -> None:
        self.settings: AdapterSettings = settings or AdapterSettings()
        self.max_retries: int = 3
        self.retry_delay: float = 1.0
        self.outcomes: list[Outcome] = list(outcomes)
        self.calls: int = 0

    def _next(self, url: str) -> Response:
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else 200
        if isinstance(outcome, Exception):
            raise outcome
        return Response(url=url, status_code=outcome, content=b"body")

    @retry()
    def download(self, url: str, **_kwargs: Any) -> Response:
        return self._next(url)

    @aretry()
    async def adownload(self, url: str, **_kwargs: Any) -> Response:
        return self._next(url)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(retry_module, "time", fake)
    return fake


@pytest.fixture
def async_clock(monkeypatch: pytest.MonkeyPatch) -> FakeAsyncClock:
    fake = FakeAsyncClock()
    monkeypatch.setattr(retry_module, "asyncio", fake)
    return fake


def test_success_is_not_retried(clock: FakeClock) -> None:
    adapter = FakeAdapter([200])
    response = adapter.download("http://e.com")

    assert adapter.calls == 1
    assert response.status_code == 200
    assert response.attempts == 1
    assert clock.slept == []


def test_retryable_status_is_retried_until_it_succeeds(clock: FakeClock) -> None:
    adapter = FakeAdapter([503, 200])
    response = adapter.download("http://e.com")

    assert adapter.calls == 2
    assert response.status_code == 200
    assert response.attempts == 2
    assert len(clock.slept) == 1
    assert get_recorder().counters.retries == {"fake:status_code": 1}


def test_allowed_status_codes_suppress_the_retry(clock: FakeClock) -> None:
    adapter = FakeAdapter([503, 200])
    response = adapter.download("http://e.com", allowed_status_codes=[503])

    assert adapter.calls == 1
    assert response.status_code == 503
    assert clock.slept == []


@pytest.mark.usefixtures("clock")
def test_non_retryable_status_is_returned_as_is() -> None:
    adapter = FakeAdapter([404, 200])
    assert adapter.download("http://e.com").status_code == 404
    assert adapter.calls == 1


def test_retries_are_capped(clock: FakeClock) -> None:
    adapter = FakeAdapter([503, 503, 503, 503])
    response = adapter.download("http://e.com", max_retries=2)

    assert adapter.calls == 3
    assert response.status_code == 503
    assert response.attempts == 3
    assert len(clock.slept) == 2


@pytest.mark.usefixtures("clock")
def test_zero_retries_means_one_attempt() -> None:
    adapter = FakeAdapter([503])
    assert adapter.download("http://e.com", max_retries=0).status_code == 503
    assert adapter.calls == 1


@pytest.mark.usefixtures("clock")
def test_exception_is_retried_then_wrapped_into_a_response() -> None:
    boom = RuntimeError("connection reset")
    adapter = FakeAdapter([boom, boom])
    response = adapter.download("http://e.com", max_retries=1)

    assert adapter.calls == 2
    assert response.status_code == -1
    assert response.exception is boom
    assert response.attempts == 2
    assert not response.ok
    assert get_recorder().counters.retries == {"fake:exception": 1}


@pytest.mark.usefixtures("clock")
def test_exception_then_success() -> None:
    adapter = FakeAdapter([RuntimeError("flaky"), 200])
    assert adapter.download("http://e.com").status_code == 200
    assert adapter.calls == 2


@pytest.mark.parametrize("error", [ValidationError("bad url"), AdapterError("missing dependency")])
def test_permanent_errors_are_not_retried(clock: FakeClock, error: Exception) -> None:
    adapter = FakeAdapter([error])
    with pytest.raises(type(error)):
        adapter.download("http://e.com")
    assert adapter.calls == 1
    assert clock.slept == []


def test_backoff_grows_exponentially_within_the_jitter_band(clock: FakeClock) -> None:
    adapter = FakeAdapter([503, 503, 503, 200], AdapterSettings(initial_backoff=1.0, backoff_exponent=2.0))
    adapter.download("http://e.com", max_retries=3, retry_delay=1.0)

    assert len(clock.slept) == 3
    for attempt, delay in enumerate(clock.slept):
        expected = 1.0 * 2.0**attempt
        assert 0.8 * expected <= delay <= 1.2 * expected


def test_backoff_is_capped_by_max_backoff(clock: FakeClock) -> None:
    adapter = FakeAdapter([503] * 6, AdapterSettings(initial_backoff=10.0, max_backoff=12.0))
    adapter.download("http://e.com", max_retries=5, retry_delay=10.0)

    assert max(clock.slept) <= 12.0 * 1.2


def test_retry_delay_can_be_a_random_range(clock: FakeClock) -> None:
    adapter = FakeAdapter([503, 200])
    adapter.download("http://e.com", retry_delay=(2.0, 4.0))

    assert 2.0 * 0.8 <= clock.slept[0] <= 4.0 * 1.2


async def test_async_retry_mirrors_the_sync_behaviour(async_clock: FakeAsyncClock) -> None:
    adapter = FakeAdapter([503, RuntimeError("flaky"), 200])
    response = await adapter.adownload("http://e.com")

    assert adapter.calls == 3
    assert response.status_code == 200
    assert response.attempts == 3
    assert len(async_clock.slept) == 2
    assert get_recorder().counters.retries == {"fake:status_code": 1, "fake:exception": 1}


async def test_async_permanent_error_is_not_retried(async_clock: FakeAsyncClock) -> None:
    adapter = FakeAdapter([ValidationError("bad")])
    with pytest.raises(ValidationError):
        await adapter.adownload("http://e.com")
    assert adapter.calls == 1
    assert async_clock.slept == []


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("", ""),
        ("() => 1", "() => 1"),
        ("async () => 1", "async () => 1"),
        ("function f() {}", "function f() {}"),
        ("document.title", "() => (document.title)"),
        ("return document.title", "() => { return document.title }"),
    ],
)
def test_normalize_js(script: str, expected: str) -> None:
    assert normalize_js(script) == expected


def test_policy_reads_the_adapter_then_the_call_kwargs() -> None:
    adapter = FakeAdapter([], AdapterSettings(max_attempts=9, initial_backoff=3.0, backoff_exponent=1.5))
    adapter.max_retries = 5
    adapter.retry_delay = 2.0

    from_adapter = RetryPolicy.resolve(adapter, {})
    assert (from_adapter.max_retries, from_adapter.base_delay) == (5, 2.0)
    assert (from_adapter.exponent, from_adapter.max_backoff) == (1.5, 30.0)

    from_call = RetryPolicy.resolve(adapter, {"max_retries": 1, "retry_delay": 0.5})
    assert (from_call.max_retries, from_call.base_delay) == (1, 0.5)


def test_policy_normalises_hostile_values() -> None:
    adapter = FakeAdapter([])
    assert RetryPolicy.resolve(adapter, {"max_retries": -3}).max_retries == 0
    assert RetryPolicy.resolve(adapter, {"max_retries": 10**9}).max_retries == HARD_MAX_RETRIES
    assert RetryPolicy.resolve(adapter, {"max_retries": "many"}).max_retries == 3
    assert RetryPolicy.resolve(adapter, {"retry_delay": (1.0, 1.0)}).base_delay == 1.0
    assert RetryPolicy.resolve(adapter, {"retry_delay": (1.0,)}).base_delay == 1.0


def test_policy_decides_which_statuses_are_worth_retrying() -> None:
    policy = RetryPolicy(retry_codes=frozenset({503}), allowed_status_codes=frozenset({429}))
    assert policy.retries_status(503) is True
    assert policy.retries_status(404) is False
    assert policy.retries_status(None) is False
    assert RetryPolicy(retry_codes=frozenset({429}), allowed_status_codes=frozenset({429})).retries_status(429) is False


def test_policy_backoff_is_capped_and_jittered() -> None:
    policy = RetryPolicy(base_delay=10.0, exponent=2.0, max_backoff=25.0)
    assert 8.0 <= policy.delay_for(0) <= 12.0
    assert 20.0 <= policy.delay_for(5) <= 30.0
    assert policy.total_attempts == 4


def test_mark_utf8_charset_rewrites_charset_but_keeps_media_type() -> None:
    """浏览器适配器把正文重编码成 UTF-8，headers 的 charset 必须跟着改。

    不改的话客户端按原站点声明的 charset（如 gb2312）去解 UTF-8 字节，必然乱码，
    而且 status 是 200、没有任何报错。
    """
    assert mark_utf8_charset({"content-type": "text/html; charset=gb2312"}) == {
        "content-type": "text/html; charset=utf-8"
    }
    # media type 要保住：xhtml/xml 丢了它会影响下游判断
    assert mark_utf8_charset({"content-type": "application/xhtml+xml; charset=big5"}) == {
        "content-type": "application/xhtml+xml; charset=utf-8"
    }
    # 大小写不敏感，且其他参数保留
    assert mark_utf8_charset({"Content-Type": "text/html; charset=gb2312; boundary=x"}) == {
        "Content-Type": "text/html; boundary=x; charset=utf-8"
    }
    # 原来没有 charset / 没有 content-type 的情况
    assert mark_utf8_charset({"content-type": "text/html"}) == {"content-type": "text/html; charset=utf-8"}
    assert mark_utf8_charset({}) == {"content-type": "text/html; charset=utf-8"}


def test_retries_stop_once_the_caller_is_gone() -> None:
    """调用方不再等待时必须放弃剩余重试。

    重试把耗时按尝试次数放大（max_retries 上限 20，配上退避总和最坏能拖到八分钟以上）。
    调用方的 deadline 早就过了还在对目标站点重投，等于凭空放大请求打到别人身上，
    而且一直占着线程池不放。
    """
    calls: list[int] = []
    alive = [True]

    @final
    class _Flaky:
        adapter_name: str = "test"
        settings: AdapterSettings = AdapterSettings()

        @retry()
        def download(self, _url: str, **_kwargs: Any) -> Response:
            calls.append(1)
            raise TransportError("boom")

    token = caller_alive_check.set(lambda: alive[0])
    try:
        # 调用方还在：按 max_attempts 打满
        result = _Flaky().download("https://example.com/", max_retries=3, retry_delay=0)
        assert len(calls) == 4
        assert result.status_code == -1

        # 调用方走了：第一次失败后就不再重试
        calls.clear()
        alive[0] = False
        result = _Flaky().download("https://example.com/", max_retries=3, retry_delay=0)
        assert len(calls) == 1
        assert result.status_code == -1
    finally:
        caller_alive_check.reset(token)


def test_caller_gone_defaults_to_false_without_a_binding() -> None:
    """没有绑定判定时（进程内直接用适配器）不得误判成"调用方走了"。"""
    assert caller_gone() is False


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        # 含 return 单词的表达式不该被包成函数体——那样返回值会静默变成 undefined
        ("document.title + ' return '", "() => (document.title + ' return ')"),
        ("o.returnValue", "() => (o.returnValue)"),
        # 真正的 return 语句仍然识别，含缩进与多行
        ("return document.title;", "() => { return document.title; }"),
        ("  return 1;", "() => { return 1; }"),
        ("const x = 1;\nreturn x;", "() => { const x = 1;\nreturn x; }"),
        # 自带函数前缀的原样透传
        ("async () => { await x(); return 1; }", "async () => { await x(); return 1; }"),
    ],
)
def test_normalize_js_only_wraps_real_return_statements(script: str, expected: str) -> None:
    """只有行首的 return 才算返回语句。

    原来用 \\breturn\\b 全文搜，于是表达式里只要出现这个单词（字符串字面量、
    o.returnValue 之类）就会被包成 () => { ... } —— 没有 return 语句，
    返回值静默变成 undefined，而请求是成功的、没有任何报错。
    """
    assert normalize_js(script) == expected


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        # 同一行里的 return 语句是函数体最常见的写法之一，不能因为它不在行首就被
        # 当成表达式——包成 () => (...) 之后是 SyntaxError，脚本整个跑不起来。
        ("if (x) return 1;", "() => { if (x) return 1; }"),
        ("const t = document.title; return t;", "() => { const t = document.title; return t; }"),
        (
            "for (const e of document.links) return e.href;",
            "() => { for (const e of document.links) return e.href; }",
        ),
        ("if (a) { return 1; } else { return 2; }", "() => { if (a) { return 1; } else { return 2; } }"),
        # 字符串字面量和注释里的 return 仍然不算返回语句（这是"只认行首"当初要挡的假阳性）
        ('"return"', '() => ("return")'),
        ("`a return b`", "() => (`a return b`)"),
        ("x + /* return */ y", "() => (x + /* return */ y)"),
        ("// return x\ndocument.title", "() => (// return x\ndocument.title)"),
    ],
)
def test_normalize_js_finds_return_statements_that_are_not_at_line_start(script: str, expected: str) -> None:
    """return 语句不一定在行首。

    "只认行首 return" 挡住了字符串里的假阳性，但同时把 ``if (x) return 1;``、
    ``const t = ...; return t;`` 这类合法函数体判成了表达式——包成 ``() => (...)``
    之后是 SyntaxError。判定改成"抹掉字符串字面量和注释之后再找 return"：
    假阳性照样挡住，真的 return 语句不管在哪一列都认。
    """
    assert normalize_js(script) == expected


@pytest.mark.parametrize(
    "error",
    [TypeError("data must be dict/list/tuple, str, BytesIO or bytes"), ValueError("bad argument")],
)
def test_permanent_parameter_errors_are_not_retried(error: Exception) -> None:
    """重试解决的是"目标站点抖了"，参数根本不合法时重试只是把失败等待按次数放大。

    实测 --impersonate nosuchbrowser999 在默认 3 次重试下要 14 秒才报错，
    data=12345 这种类型错同样白等四轮——两者都不可能因为再试一次而成立。
    """
    loop = RetryLoop(RetryPolicy(max_retries=3), "https://example.com/", "curl_cffi")

    assert loop.on_error(0, error) is None
    assert loop.last_error is error


def test_transport_errors_are_still_retried() -> None:
    """别把正常的传输失败也一起 fail-fast 了。"""
    loop = RetryLoop(RetryPolicy(max_retries=3, base_delay=0.01), "https://example.com/", "curl_cffi")

    delay = loop.on_error(0, ConnectionError("connection reset"))

    assert delay is not None and delay > 0
