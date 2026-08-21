"""逐跳重定向准入的回归测试。

SSRF 准入原本只校验调用方给的那一个 URL，适配器默认让底层库自行跟随重定向、
跟随时不再过策略——一次 ``302 Location: http://169.254.169.254/`` 就能把云元数据
取回来。这里钉住"每跳发出之前都校验"这条不变量。
"""

from __future__ import annotations

from typing import Any, final

import pytest

from ipclick.adapters.redirects import DEFAULT_MAX_REDIRECTS, follow_with_policy
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


@pytest.mark.parametrize(("status", "expected_method"), [(301, "GET"), (302, "GET"), (303, "GET"), (307, "POST"), (308, "POST")])
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
