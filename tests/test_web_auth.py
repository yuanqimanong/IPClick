from __future__ import annotations

from http import HTTPStatus
import http.client
import socket

import pytest

from ipclick.web.auth import LOCKOUT_WINDOW, MAX_FAILED_ATTEMPTS, SessionStore, WebCredentials
from ipclick.web.server import WebServer


def test_lockout_window_starts_at_the_threshold_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 0.0
    monkeypatch.setattr("ipclick.web.auth.time.monotonic", lambda: now)
    store = SessionStore()

    for _ in range(MAX_FAILED_ATTEMPTS - 1):
        store.record_failure("client")
    assert store.is_locked("client") == 0.0

    # 临近原统计窗口末尾的第五次失败，仍应得到完整锁定期。
    now = LOCKOUT_WINDOW - 1
    store.record_failure("client")
    assert store.is_locked("client") == pytest.approx(LOCKOUT_WINDOW)

    now += 1
    assert store.is_locked("client") == pytest.approx(LOCKOUT_WINDOW - 1)


def test_success_clears_failed_login_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ipclick.web.auth.time.monotonic", lambda: 10.0)
    store = SessionStore()
    for _ in range(MAX_FAILED_ATTEMPTS):
        store.record_failure("client")
    assert store.is_locked("client") > 0

    store.record_success("client")
    assert store.is_locked("client") == 0.0


def _spawn_server() -> tuple[WebServer, int]:
    """在一个空闲端口上起真实的 Web 服务，用于打 HTTP 的用例。"""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = WebServer(
        lambda: {"server": {"version": "test"}},
        WebCredentials(username="admin", password="correct-horse"),
    )
    assert server.start("127.0.0.1", port) is not None
    return server, port


def _request(port: int, method: str, path: str, body: bytes | None = None) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=body, headers={"Content-Length": str(len(body or b""))})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def test_a_credential_less_post_does_not_count_as_a_failed_login() -> None:
    """不含凭据的请求是畸形请求，不是一次"密码错误"。

    不区分的话，5 个空 body 的 POST 就能把管理员锁在门外 300 秒——而这种请求
    任何人都发得出来，不需要知道用户名。[WEB].host = 0.0.0.0 时局域网里谁都能干。
    """
    server, port = _spawn_server()
    try:
        for _ in range(MAX_FAILED_ATTEMPTS + 2):
            status, _body = _request(port, "POST", "/login", b"")
            assert status == HTTPStatus.BAD_REQUEST

        assert server.sessions.is_locked("127.0.0.1") == 0

        status, _body = _request(port, "POST", "/login", b"username=admin&password=correct-horse")
        assert status == HTTPStatus.SEE_OTHER
    finally:
        server.stop()


def test_head_answers_like_get_without_a_body() -> None:
    """实现了 GET 就该实现 HEAD——缺了它监控探针会把健康的管理端判成故障。"""
    server, port = _spawn_server()
    try:
        get_status, get_body = _request(port, "GET", "/login")
        head_status, head_body = _request(port, "HEAD", "/login")

        assert get_status == HTTPStatus.OK
        assert get_body
        assert head_status == HTTPStatus.OK
        assert head_body == b""
    finally:
        server.stop()


def test_the_handler_has_a_read_timeout() -> None:
    """没有超时的话，只发半截 body 的连接会无限期占死一个 handler 线程。"""
    server, _port = _spawn_server()
    try:
        timeout = getattr(server._make_handler(), "timeout", None)
        assert isinstance(timeout, (int, float))
        assert timeout > 0
    finally:
        server.stop()
