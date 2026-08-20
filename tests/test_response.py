from __future__ import annotations

from ipclick.dto.response import Response


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
