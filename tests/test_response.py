from __future__ import annotations

from ipclick.dto.response import Headers, Response


def test_get_encoding_accepts_case_insensitive_quoted_charset() -> None:
    response = Response(
        url="https://example.com",
        status_code=200,
        headers={"CONTENT-TYPE": 'text/html; Charset="GB18030"; boundary=x'},
    )

    assert response.get_encoding() == "GB18030"


def test_content_uses_the_declared_charset_when_text_is_missing() -> None:
    response = Response(
        url="https://example.com",
        status_code=200,
        content="中文".encode("gb18030"),
        headers={"Content-Type": "text/plain; charset=gb18030"},
    )

    assert response.text == "中文"


def test_success_response_uses_the_declared_charset() -> None:
    response = Response.success_response(
        "https://example.com",
        content="中文".encode("gb18030"),
        headers={"content-type": "text/plain; charset=gb18030"},
    )

    assert response.text == "中文"


def test_unknown_charset_falls_back_to_utf8() -> None:
    response = Response(
        url="https://example.com",
        status_code=200,
        content="中文".encode(),
        headers={"Content-Type": "text/plain; charset=not-a-real-codec"},
    )

    assert response.text == "中文"


def test_headers_lookup_is_case_insensitive_both_ways() -> None:
    """HTTP 头字段本就大小写不敏感，适配器之间的拼写差异不该泄漏给调用方。

    curl_cffi 给全小写、niquests 保留原样。用普通 dict 装的话，照一个适配器写好的
    headers.get("content-type") 换成另一个就返回 None——不报错、不告警，
    直接走进错误分支。
    """
    lower = Headers({"content-type": "application/json", "content-length": "12"})
    upper = Headers({"Content-Type": "application/json", "Content-Length": "12"})

    for headers in (lower, upper):
        assert headers.get("content-type") == "application/json"
        assert headers.get("Content-Type") == "application/json"
        assert headers["CONTENT-TYPE"] == "application/json"
        assert "cOnTeNt-TyPe" in headers
        assert headers.get("x-missing") is None
        assert headers.get("x-missing", "fallback") == "fallback"

    # 迭代与打印仍是服务端给的原始拼写
    assert sorted(upper) == ["Content-Length", "Content-Type"]
    assert sorted(lower) == ["content-length", "content-type"]


def test_headers_assignment_replaces_the_other_spelling() -> None:
    headers = Headers({"Content-Type": "text/plain"})
    headers["content-type"] = "application/json"

    assert list(headers) == ["content-type"]
    assert headers.get("Content-Type") == "application/json"
    del headers["CONTENT-TYPE"]
    assert "content-type" not in headers
