"""不可信 curl 串解析的回归测试。

这个解析器的输入是用户粘贴进 Web 沙箱页的任意文本（"照着这条 curl 复现一下"这种
场景里，那串东西是别人给的）。它的契约是：**永不抛异常**，看不懂的东西必须以
提示的形式说出来，而不是静默换成别的意思。
"""

from __future__ import annotations

import pytest

from ipclick.web.curl_parser import CURL_MAX_NOTES, parse_curl


@pytest.mark.parametrize(
    "command",
    [
        "curl -m inf https://example.com/",
        "curl --max-time 1e400 https://example.com/",
        "curl --connect-timeout inf https://example.com/",
        "curl -m -inf https://example.com/",
        "curl -m nan https://example.com/",
    ],
)
def test_a_non_finite_timeout_does_not_raise(command: str) -> None:
    """``int(float("inf"))`` 抛的是 OverflowError，原来只接了 ValueError。

    解析器抛异常的后果不是"报个错"：SandboxPage.import_curl 和 do_POST 的 /test 分支
    都没有 try/except，异常一路到 BaseHTTPRequestHandler，连接被直接关掉——实测
    客户端拿到 RemoteDisconnected，**一个字节的响应都没有**。
    """
    parsed = parse_curl(command)

    assert parsed.error == ""
    assert parsed.url == "https://example.com/"
    assert any("超时" in note for note in parsed.notes)


@pytest.mark.parametrize(
    ("command", "expected_url"),
    [
        ("curl -c cookies.txt https://real.example.com/api", "https://real.example.com/api"),
        ("curl -Lo out.html https://real.example.com/api", "https://real.example.com/api"),
        ("curl --cacert /etc/ca.pem https://real.example.com/api", "https://real.example.com/api"),
        ("curl --key id_rsa.pem --cert client.pem https://real.example.com/api", "https://real.example.com/api"),
        (
            "curl --resolve example.com:443:127.0.0.1 https://real.example.com/api",
            "https://real.example.com/api",
        ),
        ("curl -sd 'user=alice@evil.example' https://real.example.com/api", "https://real.example.com/api"),
    ],
)
def test_an_unknown_flag_does_not_steal_the_url(command: str, expected_url: str) -> None:
    """未识别的带值参数必须把它的值一起吃掉。

    原来不吃，于是那个值变成位置参数，而 _looks_like_url 只要求"含一个点"——
    实测 ``curl -c cookies.txt https://real.example.com/api`` 解析出的 url 是
    ``https://cookies.txt``；``-sd 'user=alice@evil.example'`` 更糟，目标主机变成
    了攻击者选的 evil.example，而这串东西正是"照着这条 curl 复现"给过来的。
    """
    parsed = parse_curl(command)

    assert parsed.url == expected_url


@pytest.mark.parametrize(
    "raw",
    ["v1\r\nX-Injected: pwned", "v1\x85X-Injected: pwned", "v1\u2028X-Injected: pwned", "v1\x1cX-Injected: pwned"],
)
def test_a_line_break_inside_a_header_value_is_refused(raw: str) -> None:
    """请求头的值里不能留任何 str.splitlines() 认作换行的字符。

    表单把 headers 渲染成一个 textarea，沙箱再按行切回 dict。\x85 / \x1c / U+2028 /
    U+2029 在 textarea 里**只占一行**（实测 scrollHeight 未变），所以"发送前自己看一眼"
    根本挡不住——一条粘进去的请求头会变成两条发出去的请求头。
    """
    parsed = parse_curl(f"curl -H 'X-A: {raw}' https://real.example.com/")

    assert "X-A" not in parsed.headers or "X-Injected" not in parsed.headers["X-A"]
    assert any("换行" in note for note in parsed.notes)


def test_a_line_break_inside_a_header_name_is_refused() -> None:
    parsed = parse_curl("curl -H 'X-Orig\nX-Injected: yes: v' https://real.example.com/")

    assert parsed.headers == {}
    assert any("换行" in note for note in parsed.notes)


@pytest.mark.parametrize(
    "command",
    [
        "curl -d @payload.json https://real.example.com/api",
        "curl --data-binary @body.bin https://real.example.com/",
        "curl -d @- https://real.example.com/",
    ],
)
def test_an_at_file_body_is_reported_instead_of_sent_literally(command: str) -> None:
    """``-d @file`` 不能把字面量 "@file" 当成请求体悄悄发出去。

    模块文档明说不读本地文件——问题不是不读，而是把替换后的东西当作正确的交出去，
    一句提示都没有。其余不支持的构造（-F / -u / -x / --data-urlencode）都有提示。
    """
    parsed = parse_curl(command)

    assert parsed.body == ""
    assert any("本地文件" in note for note in parsed.notes)


def test_dash_g_turns_the_body_into_a_query_string_like_curl_does() -> None:
    """``-G`` 是 curl 把 -d 的内容拼到查询串上、用 GET 发出去。

    原来 -G 未识别，于是解析成 POST + 请求体：操作员以为只是读一下，实际对目标
    发了一个带请求体的 POST。
    """
    parsed = parse_curl("curl -G -d 'a=1&b=2' https://real.example.com/api")

    assert parsed.method == "GET"
    assert parsed.body == ""
    assert parsed.url == "https://real.example.com/api?a=1&b=2"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("curl -I https://real.example.com/api", "HEAD"),
        ("curl --head https://real.example.com/api", "HEAD"),
        ("curl -XPOST -d 'a=1' https://real.example.com/api", "POST"),
        ("curl -XDELETE https://real.example.com/thing/1", "DELETE"),
    ],
)
def test_method_determining_flags_are_honoured(command: str, expected: str) -> None:
    """-I / -XVERB 决定方法，不能静默落回 GET。"""
    assert parse_curl(command).method == expected


def test_an_unsupported_method_with_a_body_does_not_claim_it_became_get() -> None:
    """提示得说实话：有请求体时落回的是 POST，不是 GET。"""
    parsed = parse_curl("curl -X FOO -d 'x=1' https://real.example.com/api")

    assert parsed.method == "POST"
    assert any("POST" in note for note in parsed.notes)


def test_max_time_is_not_overwritten_by_connect_timeout() -> None:
    """--connect-timeout 是连接超时，不该顶掉 --max-time 的总超时。"""
    assert parse_curl("curl -m 60 --connect-timeout 5 https://real.example.com/").timeout == "60"
    assert parse_curl("curl --connect-timeout 5 -m 60 https://real.example.com/").timeout == "60"
    assert parse_curl("curl --connect-timeout 5 https://real.example.com/").timeout == "5"


@pytest.mark.parametrize(
    ("command", "header", "expected"),
    [
        ("curl -H 'cookie: a=1' -b 'b=2' https://real.example.com/", "cookie", "a=1"),
        ("curl -H 'user-agent: mine' -A 'other' https://real.example.com/", "user-agent", "mine"),
    ],
)
def test_a_shorthand_flag_does_not_duplicate_a_differently_cased_header(
    command: str, header: str, expected: str
) -> None:
    """-b / -A 的 setdefault 是大小写敏感的，而 -H 原样保留大小写。

    于是 ``-H 'cookie: a=1' -b 'b=2'`` 产出 cookie 和 Cookie 两条冲突的请求头，
    curl 只会发一条。
    """
    parsed = parse_curl(command)

    assert len(parsed.headers) == 1
    assert parsed.headers[header] == expected


def test_notes_are_capped_so_an_unknown_flag_storm_cannot_blow_up_the_page() -> None:
    """提示条数要有上限：去重是 O(n) 线性扫描，n 是不同提示的条数。

    实测 8000 个互不相同的未识别参数（刚好在 64 KiB 输入上限内）要 0.65 秒 CPU、
    渲染出 390 KB 的 HTML。
    """
    command = "curl " + " ".join(f"--bogus{i}" for i in range(2000)) + " https://real.example.com/"

    parsed = parse_curl(command)

    assert len(parsed.notes) <= CURL_MAX_NOTES
    assert any("提示过多" in note for note in parsed.notes)


def test_multiple_urls_are_reported_rather_than_silently_dropped() -> None:
    parsed = parse_curl("curl https://a.example.com/ https://b.example.com/")

    assert parsed.url == "https://a.example.com/"
    assert any("多个网址" in note for note in parsed.notes)
