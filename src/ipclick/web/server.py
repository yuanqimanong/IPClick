"""Web 管理端。

``ipclick run --web`` 起一个带登录的网页界面，展示服务端运行状态、生效配置与
集群节点。只用标准库——为一个几百行的页面拉进 FastAPI 那一串依赖不划算，
而且这个包刚把核心依赖精简到 17 个。

能做什么、不能做什么
--------------------
**能**：看服务端信息、看生效配置（机密只显示"有/无"）、看集群节点健康状态、
手动摘除/恢复节点（仅影响当前进程的运行时状态）。

**不能**：改配置文件、改令牌、改 URL 策略、加删节点。这些一律走配置文件 + 重启。

这条线是刻意划的。这个服务能代任意 URL 发请求，一个能改它配置的网页就是极高
价值的目标；而"手动摘个节点"这类操作是运行时的、可逆的、重启即复原，风险和
收益的比值完全不同。
"""

from collections.abc import Callable
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from typing import Any
from urllib.parse import parse_qs

from typing_extensions import override

from ipclick.utils.log_util import log
from ipclick.web.auth import SessionStore, WebCredentials
from ipclick.web.templates import render_dashboard, render_login


#: 会话 cookie 名
COOKIE_NAME = "ipclick_session"

#: 单次请求体上限。登录表单撑死几百字节，给 64KB 已经很宽松——
#: 不设上限的话一个大 POST 就能把内存吃掉。
MAX_BODY_BYTES = 64 * 1024


class WebConfig:
    """``[WEB]`` 配置。"""

    def __init__(self, config: dict[str, Any] | None = None):
        data = dict(config or {})
        self.enabled: bool = bool(data.get("enabled", False))
        self.port: int = _as_int(data.get("port"), 9530)
        # 默认只监听本机。管理界面暴露的是运行状态与节点拓扑，
        # 而且它后面就是一个能代发任意请求的服务——不该默认对外。
        self.host: str = str(data.get("host") or "127.0.0.1").strip() or "127.0.0.1"
        self.username: str = str(data.get("username") or "").strip()
        self.password: str = str(data.get("password") or "")

    def as_credentials_config(self) -> dict[str, Any]:
        return {"username": self.username, "password": self.password}


def _as_int(value: Any, default: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result if 0 < result < 65536 else default


class WebServer:
    """带登录的 Web 管理端。

    ``snapshot_provider`` 每次请求时被调用，返回要展示的数据；
    ``action_handler`` 处理可变操作，返回 ``(是否成功, 提示语)``。
    两者都由调用方注入，Web 层本身不碰业务对象。
    """

    def __init__(
        self,
        snapshot_provider: Callable[[], dict[str, Any]],
        credentials: WebCredentials,
        *,
        action_handler: Callable[[str, dict[str, str]], tuple[bool, str]] | None = None,
        sessions: SessionStore | None = None,
    ):
        self._provider: Callable[[], dict[str, Any]] = snapshot_provider
        self._credentials: WebCredentials = credentials
        self._actions: Callable[[str, dict[str, str]], tuple[bool, str]] | None = action_handler
        self.sessions: SessionStore = sessions or SessionStore()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def start(self, host: str = "127.0.0.1", port: int = 9530) -> str | None:
        """启动。返回访问地址；起不来返回 None。"""
        if self._httpd is not None:
            return f"http://{host}:{port}/"
        try:
            self._httpd = ThreadingHTTPServer((host, port), self._make_handler())
        except OSError as e:
            log.error(f"Web 管理端启动失败 {host}:{port}: {e}")
            return None

        if host not in ("127.0.0.1", "::1", "localhost"):
            log.warning(
                f"Web 管理端监听 {host}（非回环地址）且为明文 HTTP——登录密码会在网络上明文传输。"
                "请放在做了 TLS 终止的反向代理之后，或只监听 127.0.0.1 后用 SSH 隧道访问"
            )

        self._thread = threading.Thread(target=self._httpd.serve_forever, name="ipclick-web", daemon=True)
        self._thread.start()
        return f"http://{host}:{port}/"

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        self._thread = None

    # ------------------------------------------------------------------ #
    # 请求处理
    # ------------------------------------------------------------------ #

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            server_version: str = "IPClickWeb/1.0"

            @override
            def log_message(self, format: str, *args: Any) -> None:
                """默认实现直接打到 stderr，会绕过本项目的日志配置。"""

            # -------------------- 基础工具 -------------------- #

            @property
            def source(self) -> str:
                return self.client_address[0] if self.client_address else "unknown"

            def _session_id(self) -> str | None:
                raw = self.headers.get("Cookie")
                if not raw:
                    return None
                cookie = SimpleCookie()
                try:
                    cookie.load(raw)
                except Exception:
                    return None
                morsel = cookie.get(COOKIE_NAME)
                return morsel.value if morsel else None

            def _send(
                self,
                status: int,
                body: bytes,
                content_type: str = "text/html; charset=utf-8",
                extra_headers: list[tuple[str, str]] | None = None,
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                # 管理界面不该被嵌进别人的页面里（点击劫持）
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                # 页面里没有任何外部资源，把 CSP 收到最紧
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'",
                )
                for key, value in extra_headers or []:
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def _redirect(self, location: str, extra_headers: list[tuple[str, str]] | None = None) -> None:
                self._send(
                    HTTPStatus.SEE_OTHER,
                    b"",
                    "text/plain; charset=utf-8",
                    [("Location", location), *(extra_headers or [])],
                )

            def _read_form(self) -> dict[str, str]:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return {}
                if length <= 0 or length > MAX_BODY_BYTES:
                    return {}
                raw = self.rfile.read(length).decode("utf-8", errors="replace")
                return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

            # -------------------- 路由 -------------------- #

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0].rstrip("/") or "/"
                session = server.sessions.get(self._session_id())

                if path == "/login":
                    if session is not None:
                        return self._redirect("/")
                    return self._send(HTTPStatus.OK, render_login().encode())

                if session is None:
                    return self._redirect("/login")

                if path == "/":
                    snapshot = server._safe_snapshot()
                    body = render_dashboard(snapshot, session.username, session.csrf_token, bool(server._actions))
                    return self._send(HTTPStatus.OK, body.encode())

                if path == "/api/status":
                    payload = json.dumps(server._safe_snapshot(), ensure_ascii=False, indent=2, default=str)
                    return self._send(HTTPStatus.OK, payload.encode(), "application/json; charset=utf-8")

                return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")

            def do_POST(self) -> None:
                path = self.path.split("?", 1)[0].rstrip("/") or "/"

                if path == "/login":
                    return self._handle_login()

                # 其余 POST 一律要求已登录 + CSRF
                session_id = self._session_id()
                session = server.sessions.get(session_id)
                if session is None:
                    return self._redirect("/login")

                form = self._read_form()
                if not server.sessions.check_csrf(session_id, form.get("csrf_token")):
                    log.warning(f"Web 端 CSRF 校验失败，来源 {self.source}")
                    return self._send(HTTPStatus.FORBIDDEN, "CSRF 校验失败".encode(), "text/plain; charset=utf-8")

                if path == "/logout":
                    server.sessions.destroy(session_id)
                    return self._redirect(
                        "/login", [("Set-Cookie", f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")]
                    )

                if path == "/action":
                    return self._handle_action(form)

                return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")

            def _handle_login(self) -> None:
                locked_for = server.sessions.is_locked(self.source)
                if locked_for > 0:
                    body = render_login(error=f"失败次数过多，请 {locked_for:.0f} 秒后再试")
                    return self._send(HTTPStatus.TOO_MANY_REQUESTS, body.encode())

                form = self._read_form()
                username = form.get("username", "")
                password = form.get("password", "")
                if not server._credentials.verify(username, password):
                    server.sessions.record_failure(self.source)
                    # 不区分"用户名不存在"和"密码错误"——那等于给撞库确认用户名
                    return self._send(HTTPStatus.UNAUTHORIZED, render_login(error="用户名或密码错误").encode())

                server.sessions.record_success(self.source)
                session_id, _ = server.sessions.create(username)
                log.info(f"Web 端登录成功：{username}（来自 {self.source}）")
                # HttpOnly 挡 XSS 偷 cookie；SameSite=Strict 挡跨站发起的请求
                cookie = f"{COOKIE_NAME}={session_id}; Path=/; HttpOnly; SameSite=Strict"
                return self._redirect("/", [("Set-Cookie", cookie)])

            def _handle_action(self, form: dict[str, str]) -> None:
                if server._actions is None:
                    return self._send(
                        HTTPStatus.FORBIDDEN, "本实例未开放任何操作".encode(), "text/plain; charset=utf-8"
                    )
                name = form.get("action", "")
                try:
                    ok, message = server._actions(name, form)
                except Exception as e:
                    log.exception(f"Web 端操作 {name!r} 失败：{e}")
                    ok, message = False, f"操作失败：{type(e).__name__}"
                log.info(f"Web 端操作 {name!r} -> {'成功' if ok else '失败'}：{message}")
                return self._redirect("/")

        return Handler

    def _safe_snapshot(self) -> dict[str, Any]:
        try:
            return self._provider()
        except Exception as e:
            log.exception(f"取 Web 端展示数据失败：{e}")
            return {"error": f"{type(e).__name__}: {e}"}


__all__ = ["COOKIE_NAME", "WebConfig", "WebServer"]
