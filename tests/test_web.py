"""Web 管理端：凭据、会话、CSRF、登录限速、页面渲染。

安全相关的部分起真实 HTTP 服务端跑真实请求——这类东西"看起来实现了"和
"真的挡住了"之间的差距，只有真打一遍才知道。
"""

from http.cookiejar import CookieJar
import json
import re
import socket
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

import pytest

from ipclick.web.auth import (
    LOCKOUT_WINDOW,
    MAX_FAILED_ATTEMPTS,
    SessionStore,
    WebCredentials,
    generate_password,
)
from ipclick.web.server import WebConfig, WebServer
from ipclick.web.templates import esc, render_dashboard, render_login


_SNAPSHOT: dict[str, Any] = {
    "server": {
        "address": "127.0.0.1:9527",
        "version": "0.0.0",
        "mode": "standalone",
        "max_workers": 100,
        "default_adapter": "curl_cffi",
        "adapters": ["curl_cffi", "niquests"],
    },
    "security": {
        "tls": "未启用（明文）",
        "auth": True,
        "block_private_networks": True,
        "block_metadata_endpoints": True,
    },
    "limits": {"per_host_max_concurrent": 4, "per_host_qps": 2, "backend": "memory"},
    "browser": {"engine": "camoufox", "max_pages": 4, "allow_scripts": False},
    "cluster": {
        "nodes": [
            {"id": "n1", "address": "10.0.0.1:9527", "status": "healthy", "total_requests": 5, "total_failures": 0},
            {
                "id": "n2",
                "address": "10.0.0.2:9527",
                "status": "unhealthy",
                "total_requests": 2,
                "total_failures": 2,
                "last_error": "connect refused",
            },
        ]
    },
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---------------------------------------------------------------------- #
# 凭据
# ---------------------------------------------------------------------- #


class TestCredentials:
    def test_generated_password_is_long_and_random(self):
        a, b = generate_password(), generate_password()
        assert len(a) == 20
        assert a != b

    def test_generated_password_is_alphanumeric(self):
        """要从控制台复制粘贴，掺标点会在各种 shell 里被转义或截断，
        反而逼人绕过它去设弱口令。"""
        assert generate_password().isalnum()

    def test_no_password_means_generated_not_default(self):
        """默认弱口令是这类管理界面被打穿的头号原因，绝不能有。"""
        creds = WebCredentials.resolve({})
        assert creds.generated is True
        assert creds.password not in ("", "admin", "password", "ipclick")
        assert len(creds.password) >= 16

    def test_config_password_wins(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("IPCLICK_WEB_PASSWORD", raising=False)
        creds = WebCredentials.resolve({"username": "ops", "password": "s3cret"})
        assert (creds.username, creds.password, creds.generated) == ("ops", "s3cret", False)

    def test_env_beats_config(self, monkeypatch: pytest.MonkeyPatch):
        """密码不该写进会进版本库的配置文件，所以环境变量优先。"""
        monkeypatch.setenv("IPCLICK_WEB_USER", "envuser")
        monkeypatch.setenv("IPCLICK_WEB_PASSWORD", "envpass")
        creds = WebCredentials.resolve({"username": "cfg", "password": "cfgpass"})
        assert (creds.username, creds.password) == ("envuser", "envpass")

    def test_default_username_is_admin(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("IPCLICK_WEB_USER", raising=False)
        assert WebCredentials.resolve({}).username == "admin"

    def test_verify(self):
        creds = WebCredentials(username="u", password="p")
        assert creds.verify("u", "p")
        assert not creds.verify("u", "wrong")
        assert not creds.verify("wrong", "p")
        assert not creds.verify("", "")


class TestSessionStore:
    def test_create_and_get(self):
        store = SessionStore()
        sid, csrf = store.create("alice")
        session = store.get(sid)
        assert session is not None
        assert session.username == "alice"
        assert session.csrf_token == csrf

    def test_session_ids_are_unpredictable(self):
        store = SessionStore()
        ids = {store.create("u")[0] for _ in range(20)}
        assert len(ids) == 20
        assert all(len(i) >= 32 for i in ids)

    def test_unknown_session(self):
        assert SessionStore().get("nope") is None
        assert SessionStore().get(None) is None

    def test_expiry(self):
        store = SessionStore(ttl=0.05)
        sid, _ = store.create("u")
        assert store.get(sid) is not None
        time.sleep(0.1)
        assert store.get(sid) is None, "过期会话必须失效"

    def test_destroy(self):
        store = SessionStore()
        sid, _ = store.create("u")
        store.destroy(sid)
        assert store.get(sid) is None

    def test_csrf_check(self):
        store = SessionStore()
        sid, csrf = store.create("u")
        assert store.check_csrf(sid, csrf)
        assert not store.check_csrf(sid, "wrong")
        assert not store.check_csrf(sid, None)
        assert not store.check_csrf("bad-session", csrf)

    def test_csrf_tokens_differ_per_session(self):
        store = SessionStore()
        assert store.create("u")[1] != store.create("u")[1]


class TestLockout:
    def test_locks_after_threshold(self):
        store = SessionStore()
        for _ in range(MAX_FAILED_ATTEMPTS - 1):
            store.record_failure("1.2.3.4")
        assert store.is_locked("1.2.3.4") == 0.0
        store.record_failure("1.2.3.4")
        assert 0 < store.is_locked("1.2.3.4") <= LOCKOUT_WINDOW

    def test_lock_is_per_source(self):
        store = SessionStore()
        for _ in range(MAX_FAILED_ATTEMPTS):
            store.record_failure("1.2.3.4")
        assert store.is_locked("5.6.7.8") == 0.0

    def test_success_clears_failures(self):
        store = SessionStore()
        for _ in range(MAX_FAILED_ATTEMPTS - 1):
            store.record_failure("1.2.3.4")
        store.record_success("1.2.3.4")
        store.record_failure("1.2.3.4")
        assert store.is_locked("1.2.3.4") == 0.0


# ---------------------------------------------------------------------- #
# 模板
# ---------------------------------------------------------------------- #


class TestTemplates:
    def test_escapes_html(self):
        assert esc('<script>"x"</script>') == "&lt;script&gt;&quot;x&quot;&lt;/script&gt;"

    def test_node_id_is_escaped(self):
        """节点 id 来自配置或 DNS，直接拼进 HTML 就是注入点。"""
        snapshot = json.loads(json.dumps(_SNAPSHOT))
        snapshot["cluster"]["nodes"][0]["id"] = "<img src=x onerror=alert(1)>"
        html = render_dashboard(snapshot, "admin", "tok", True)
        assert "<img src=x" not in html
        assert "&lt;img" in html

    def test_error_message_is_escaped(self):
        snapshot = json.loads(json.dumps(_SNAPSHOT))
        snapshot["cluster"]["nodes"][1]["last_error"] = "<b>boom</b>"
        html = render_dashboard(snapshot, "admin", "tok", True)
        assert "<b>boom</b>" not in html

    def test_dashboard_has_csrf_in_forms(self):
        html = render_dashboard(_SNAPSHOT, "admin", "my-token", True)
        assert 'name="csrf_token" value="my-token"' in html

    def test_no_actions_means_no_action_buttons(self):
        html = render_dashboard(_SNAPSHOT, "admin", "tok", False)
        assert 'name="action"' not in html

    def test_login_page_has_no_external_resources(self):
        """CSP 收得很紧，页面里不能有任何外链资源，否则自己把自己挡了。"""
        html = render_login()
        assert "http://" not in html and "https://" not in html

    def test_dashboard_states_the_secret_boundary(self):
        """边界声明必须在页面上。

        0.3 起配置**可以**从网页改（会写回 toml），但机密仍然一律不显示、
        不接受写入——页面上要把这条边界说清楚，否则没人知道该去哪儿改令牌。
        """
        html = render_dashboard(_SNAPSHOT, "admin", "t", True)
        assert "机密" in html
        assert ".env" in html

    def test_dashboard_has_nav_to_every_page(self):
        html = render_dashboard(_SNAPSHOT, "admin", "t", True)
        for path in ("/trace", "/test", "/config", "/nodes"):
            assert f'href="{path}"' in html


class TestWebConfig:
    def test_defaults(self):
        c = WebConfig({})
        assert c.enabled is False
        assert c.port == 9530
        assert c.host == "127.0.0.1", "管理界面不该默认对外"

    def test_bad_port_falls_back(self):
        assert WebConfig({"port": "abc"}).port == 9530
        assert WebConfig({"port": 99999}).port == 9530


# ---------------------------------------------------------------------- #
# 真实 HTTP
# ---------------------------------------------------------------------- #


class _Client:
    """带 cookie 的极简 HTTP 客户端。"""

    def __init__(self, base: str):
        self.base = base
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def get(self, path: str) -> tuple[int, str, str]:
        try:
            r = self.opener.open(self.base + path, timeout=10)
            return r.status, r.read().decode(), r.geturl()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), path

    def post(self, path: str, data: dict[str, str]) -> tuple[int, str, str]:
        body = urllib.parse.urlencode(data).encode()
        try:
            r = self.opener.open(urllib.request.Request(self.base + path, data=body), timeout=10)
            return r.status, r.read().decode(), r.geturl()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), path

    def csrf(self) -> str:
        _, html, _ = self.get("/")
        match = re.search(r'name="csrf_token" value="([^"]+)"', html)
        return match.group(1) if match else ""


@pytest.fixture
def web() -> Any:
    actions: list[tuple[str, dict[str, str]]] = []

    def handler(name: str, form: dict[str, str]) -> tuple[bool, str]:
        actions.append((name, form))
        return True, "ok"

    creds = WebCredentials(username="admin", password="test-password")
    server = WebServer(lambda: _SNAPSHOT, creds, action_handler=handler)
    port = _free_port()
    url = server.start("127.0.0.1", port)
    assert url is not None
    try:
        yield _Client(f"http://127.0.0.1:{port}"), server, actions
    finally:
        server.stop()


class TestHttpFlow:
    def test_anonymous_is_redirected_to_login(self, web):
        client, _, _ = web
        status, body, url = client.get("/")
        assert status == 200
        assert url.endswith("/login")
        assert 'name="password"' in body

    def test_wrong_password_rejected(self, web):
        client, _, _ = web
        status, body, _ = client.post("/login", {"username": "admin", "password": "nope"})
        assert status == 401
        assert "用户名或密码错误" in body

    def test_error_does_not_reveal_which_field_was_wrong(self, web):
        """区分"用户名不存在"和"密码错误"等于给撞库确认用户名。"""
        client, _, _ = web
        _, bad_user, _ = client.post("/login", {"username": "nobody", "password": "x"})
        _, bad_pass, _ = client.post("/login", {"username": "admin", "password": "x"})
        assert re.findall(r'class="err">([^<]+)', bad_user) == re.findall(r'class="err">([^<]+)', bad_pass)

    def test_login_then_dashboard(self, web):
        client, _, _ = web
        status, body, url = client.post("/login", {"username": "admin", "password": "test-password"})
        assert status == 200
        assert url.endswith("/")
        assert "IPClick 管理端" in body

    def test_session_cookie_is_httponly_and_samesite(self, web):
        """HttpOnly 挡 XSS 偷 cookie，SameSite=Strict 挡跨站发起的请求。"""
        client, _, _ = web
        client.post("/login", {"username": "admin", "password": "test-password"})
        cookies = [c for c in client.jar if c.name == "ipclick_session"]
        assert cookies, "登录后应下发会话 cookie"
        # CookieJar 不直接暴露 HttpOnly，检查原始属性
        assert cookies[0].has_nonstandard_attr("HttpOnly")

    def test_api_status_requires_login(self, web):
        client, _, _ = web
        _, _, url = client.get("/api/status")
        assert url.endswith("/login")

    def test_api_status_after_login(self, web):
        client, _, _ = web
        client.post("/login", {"username": "admin", "password": "test-password"})
        status, body, _ = client.get("/api/status")
        assert status == 200
        assert sorted(json.loads(body)) == ["browser", "cluster", "limits", "security", "server"]

    def test_security_headers_present(self, web):
        client, _, _ = web
        raw = urllib.request.urlopen(client.base + "/login", timeout=10)
        headers = {k.lower(): v for k, v in raw.getheaders()}
        assert headers["x-frame-options"] == "DENY"
        assert headers["x-content-type-options"] == "nosniff"
        assert "default-src 'none'" in headers["content-security-policy"]
        assert headers["cache-control"] == "no-store"

    def test_post_without_csrf_is_rejected(self, web):
        client, _, _ = web
        client.post("/login", {"username": "admin", "password": "test-password"})
        status, body, _ = client.post("/logout", {})
        assert status == 403
        assert "CSRF" in body

    def test_post_with_wrong_csrf_is_rejected(self, web):
        client, _, _ = web
        client.post("/login", {"username": "admin", "password": "test-password"})
        status, _, _ = client.post("/logout", {"csrf_token": "forged"})
        assert status == 403

    def test_logout_invalidates_session(self, web):
        client, _, _ = web
        client.post("/login", {"username": "admin", "password": "test-password"})
        client.post("/logout", {"csrf_token": client.csrf()})
        _, body, url = client.get("/")
        assert url.endswith("/login")
        assert 'name="password"' in body

    def test_action_requires_login(self, web):
        client, _, actions = web
        client.post("/action", {"action": "drain", "node_id": "n1"})
        assert actions == [], "未登录也能触发操作就等于没有鉴权"

    def test_action_requires_csrf(self, web):
        client, _, actions = web
        client.post("/login", {"username": "admin", "password": "test-password"})
        client.post("/action", {"action": "drain", "node_id": "n1"})
        assert actions == []

    def test_action_with_csrf_is_dispatched(self, web):
        client, _, actions = web
        client.post("/login", {"username": "admin", "password": "test-password"})
        client.post("/action", {"action": "drain", "node_id": "n1", "csrf_token": client.csrf()})
        assert actions and actions[-1][0] == "drain"
        assert actions[-1][1]["node_id"] == "n1"

    def test_lockout_after_repeated_failures(self, web):
        client, _, _ = web
        for _ in range(MAX_FAILED_ATTEMPTS):
            client.post("/login", {"username": "admin", "password": "nope"})
        status, body, _ = client.post("/login", {"username": "admin", "password": "nope"})
        assert status == 429
        assert "失败次数过多" in body

    def test_locked_out_even_with_correct_password(self, web):
        """锁定期间正确密码也不放行——否则限速对"最后一次猜对"毫无作用。"""
        client, _, _ = web
        for _ in range(MAX_FAILED_ATTEMPTS):
            client.post("/login", {"username": "admin", "password": "nope"})
        status, _, _ = client.post("/login", {"username": "admin", "password": "test-password"})
        assert status == 429

    def test_unknown_path_is_404(self, web):
        client, _, _ = web
        client.post("/login", {"username": "admin", "password": "test-password"})
        status, _, _ = client.get("/nope")
        assert status == 404

    def test_snapshot_failure_does_not_crash_page(self):
        """取状态失败也要出页面，不能白屏——运维正是在出问题时才来看这个。"""

        def boom() -> dict[str, Any]:
            raise RuntimeError("provider down")

        creds = WebCredentials(username="admin", password="p")
        server = WebServer(boom, creds)
        port = _free_port()
        assert server.start("127.0.0.1", port)
        try:
            client = _Client(f"http://127.0.0.1:{port}")
            client.post("/login", {"username": "admin", "password": "p"})
            status, body, _ = client.get("/")
            assert status == 200
            assert "取状态失败" in body
        finally:
            server.stop()
