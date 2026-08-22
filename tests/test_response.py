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


def test_headers_stay_case_insensitive_after_inherited_mutations() -> None:
    """update / pop / setdefault / |= 都是继承来的，原来会绕过小写索引。

    这个类存在的意义就是"两种拼写都取得到"。而索引只在 __setitem__ / __delitem__ 里
    维护，于是 h.update({"set-cookie": ...}) 之后 h.get("Set-Cookie") 又变回 None——
    正是它要防的那个失败。
    """
    headers = Headers({"Content-Type": "text/html"})
    headers.update({"set-cookie": "s=1"})

    assert headers.get("Set-Cookie") == "s=1"
    assert "SET-COOKIE" in headers

    headers |= {"X-A": "1"}
    assert headers.get("x-a") == "1"

    assert headers.setdefault("x-a", "ignored") == "1"
    assert headers.setdefault("X-B", "2") == "2"
    assert headers.get("x-b") == "2"


def test_headers_get_with_a_default_never_raises_after_pop() -> None:
    """带默认值的 get 永远不该抛。

    继承来的 dict.pop 把键删了却留下索引条目，于是 "content-type" in h 为真、
    而 h.get("content-type", "X") 抛 KeyError。
    """
    headers = Headers({"Content-Type": "text/html"})

    assert headers.pop("content-type") == "text/html"
    assert "content-type" not in headers
    assert headers.get("content-type", "fallback") == "fallback"

    headers2 = Headers({"X-A": "1"})
    headers2.clear()
    assert headers2.get("x-a", "fallback") == "fallback"


def test_headers_copy_keeps_the_type() -> None:
    """继承 dict.copy 会退化成普通 dict，静默丢掉大小写不敏感。"""
    copied = Headers({"Content-Type": "text/html"}).copy()

    assert isinstance(copied, Headers)
    assert copied.get("content-type") == "text/html"


def test_one_field_written_in_two_spellings_collapses() -> None:
    """同一字段的不同拼写不能并存，否则迭代出去会发两遍。"""
    headers = Headers({"Set-Cookie": "a"})
    headers["set-cookie"] = "b"

    assert dict(headers) == {"set-cookie": "b"}


def test_a_non_mapping_headers_value_still_yields_the_body_as_text() -> None:
    """兜底分支要给出正文，而不是 b'...' 的 repr。"""
    response = Response(url="https://example.com", status_code=200, content=b"hello", headers=["not", "a", "mapping"])  # pyright: ignore[reportArgumentType]

    assert response.text == "hello"
