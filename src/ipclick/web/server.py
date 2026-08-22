"""基于标准库 HTTP server 的轻量 Web 管理端与安全路由边界。"""

from collections.abc import Callable
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
import threading
from typing import Any, ClassVar, final
from urllib.parse import parse_qs, quote

from typing_extensions import override

from ipclick.ports import DEFAULT_WEB_PORT
from ipclick.utils.coerce import as_bool, as_int, as_text
from ipclick.utils.log_util import log
from ipclick.web.assets import csp
from ipclick.web.auth import SessionStore, WebCredentials
from ipclick.web.pages import WebPages
from ipclick.web.templates import dashboard_live, render_dashboard, render_login, set_default_theme


COOKIE_NAME = "ipclick_session"

MAX_BODY_BYTES = 256 * 1024

MAX_PORT = 65535

_CSP = csp()


@final
class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    @override
    def handle_error(self, request: Any, client_address: Any) -> None:
        """忽略正常断连噪音，并记录其余请求处理异常。"""
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            log.debug(f"Web 端客户端提前断开：{client_address}")
            return
        log.exception(f"Web 端处理请求出错（来自 {client_address}）：{error}")


THEMES: tuple[str, ...] = ("light", "dark")

DEFAULT_THEME = "light"

WILDCARD_HOSTS: frozenset[str] = frozenset({"0.0.0.0", "::", "[::]", "*"})


def normalize_theme(value: Any) -> str:
    """将任意配置值规范为受支持的主题名。"""
    text = str(value or "").strip().lower()
    return text if text in THEMES else DEFAULT_THEME


def is_public_host(host: str) -> bool:
    """判断监听地址是否可能被本机回环之外访问。"""
    return host.strip().lower() not in ("127.0.0.1", "::1", "[::1]", "localhost", "")


class WebConfig:
    """从宽松配置字典解析出的 Web 管理端设置。"""

    def __init__(self, config: dict[str, Any] | None = None):
        data = dict(config or {})
        self.enabled: bool = as_bool(data.get("enabled"))
        self.port: int = as_int(data.get("port"), DEFAULT_WEB_PORT, minimum=1, maximum=MAX_PORT)
        self.host: str = as_text(data.get("host"), "127.0.0.1")
        self.username: str = as_text(data.get("username"))
        self.password: str = str(data.get("password") or "")
        self.theme: str = normalize_theme(data.get("theme"))

    def as_credentials_config(self) -> dict[str, Any]:
        """返回凭据解析器所需的最小配置。"""
        return {"username": self.username, "password": self.password}


class WebServer:
    """提供登录、CSRF 防护、管理页面和 JSON/fragment 接口的 HTTP 服务。"""

    def __init__(
        self,
        snapshot_provider: Callable[[], dict[str, Any]],
        credentials: WebCredentials,
        *,
        action_handler: Callable[[str, dict[str, str]], tuple[bool, str]] | None = None,
        sessions: SessionStore | None = None,
        pages: WebPages | None = None,
        live_provider: Callable[[], dict[str, Any]] | None = None,
        theme: str = DEFAULT_THEME,
    ):
        self._provider: Callable[[], dict[str, Any]] = snapshot_provider
        self._credentials: WebCredentials = credentials
        self._actions: Callable[[str, dict[str, str]], tuple[bool, str]] | None = action_handler
        self.sessions: SessionStore = sessions or SessionStore()
        self.pages: WebPages | None = pages
        self._live: Callable[[], dict[str, Any]] = live_provider or snapshot_provider
        self.theme: str = normalize_theme(theme)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self, host: str = "127.0.0.1", port: int = DEFAULT_WEB_PORT) -> str | None:
        """启动后台 HTTP 线程，成功时返回管理地址。"""
        if self._httpd is not None:
            return f"http://{host}:{port}/"
        set_default_theme(self.theme)
        try:
            self._httpd = _QuietThreadingHTTPServer((host, port), self._make_handler())
        except OSError as e:
            log.error(f"Web 管理端启动失败 {host}:{port}: {e}")
            return None

        if is_public_host(host):
            scope = "所有网卡" if host.strip() in WILDCARD_HOSTS else host
            log.warning(
                f"Web 管理端监听 {scope}（非回环地址）且为明文 HTTP——登录密码会在网络上明文传输。"
                "局域网内自用可以接受；要跨网段暴露请放在做了 TLS 终止的反向代理之后，"
                "或改回只监听 127.0.0.1 后用 SSH 隧道访问"
            )
            log.warning(
                "同时请确认 [SECURITY].auth_token 已配置：这个界面的「试一试」能代发任意请求，"
                "而 gRPC 端口若没开鉴权，同一网段的人绕过网页也能直接调用"
            )

        self._thread = threading.Thread(target=self._httpd.serve_forever, name="ipclick-web", daemon=True)
        self._thread.start()
        return f"http://{host}:{port}/"

    def stop(self) -> None:
        """幂等关闭 HTTP 服务及监听 socket。"""
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        self._thread = None

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            """绑定外层 ``WebServer`` 状态的单请求处理器。"""

            server_version: str = "IPClickWeb/1.0"

            # 读请求行与 body 的超时。不设的话（标准库默认 None），一条声明了
            # Content-Length 却只发半截 body 的连接会让 rfile.read() 无限期阻塞，
            # 每条这样的连接占死一个 handler 线程。默认只监听 127.0.0.1 时影响有限，
            # 但 [WEB].host = 0.0.0.0 / --web-lan 之后局域网里谁都能这么占。
            timeout: ClassVar[float | None] = 30.0

            # do_HEAD 把它置上，_send 据此略过响应体。
            _head_only: bool = False

            @override
            def log_message(self, format: str, *args: Any) -> None:
                """关闭标准库逐请求 stderr 日志，统一使用项目日志。"""
                pass

            @property
            def source(self) -> str:
                """返回 TCP 对端地址；不信任可伪造的转发请求头。"""
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
                # 所有响应统一施加浏览器安全头；CSP 将脚本限制为内置 hash，并限制同源连接与表单。
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", _CSP)
                for key, value in extra_headers or []:
                    self.send_header(key, value)
                self.end_headers()
                # HEAD 要给出与 GET 完全相同的状态码和响应头（含 Content-Length），只是不带体。
                if not self._head_only:
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

            def _query(self) -> dict[str, str]:
                _, _, raw = self.path.partition("?")
                return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

            def do_GET(self) -> None:
                """处理只读页面、fragment 与 JSON 查询。"""
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

                if path == "/fragment/dashboard":
                    return self._send(HTTPStatus.OK, dashboard_live(server._safe_live()).encode())

                pages = server.pages
                if pages is None:
                    return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")

                if path == "/trace":
                    body = pages.trace_page(self._query(), session.username, session.csrf_token)
                    return self._send(HTTPStatus.OK, body.encode())

                if path == "/fragment/trace":
                    return self._send(HTTPStatus.OK, pages.trace_fragment(self._query()).encode())

                if path == "/api/trace":
                    payload = json.dumps(pages.trace_json(self._query()), ensure_ascii=False, indent=2, default=str)
                    return self._send(HTTPStatus.OK, payload.encode(), "application/json; charset=utf-8")

                if path == "/test":
                    form, result = pages.take_test_result(self._query().get("r", ""))
                    body = pages.test_page(form, result, session.username, session.csrf_token)
                    return self._send(HTTPStatus.OK, body.encode())

                if path == "/components":
                    body = pages.components_page(
                        session.username, session.csrf_token, node_id=self._query().get("node", "")
                    )
                    return self._send(HTTPStatus.OK, body.encode())

                if path == "/api/components/status":
                    return self._json({"job": pages.component_status(self._query().get("node", ""))})

                if path == "/config":
                    query = self._query()
                    body = pages.config_page(
                        session.username,
                        session.csrf_token,
                        # 一次性凭据取一次就没了，所以不在 HEAD 上兑付：预取器、链接
                        # 检查器、反向代理都可能先 HEAD 一遍生成后跳转的目标地址，那会
                        # 把值烧掉，管理员随后用浏览器打开只剩一个不带凭据的页面。
                        generated_token="" if self._head_only else query.get("g", ""),
                        tab=query.get("tab", "basic"),
                    )
                    return self._send(HTTPStatus.OK, body.encode())

                if path == "/nodes":
                    return self._redirect("/config?tab=cluster")

                if path == "/deploy":
                    return self._deploy(pages, session)

                if path == "/deploy.zip":
                    return self._send(
                        HTTPStatus.OK,
                        pages.deploy_bundle(),
                        "application/zip",
                        [("Content-Disposition", 'attachment; filename="ipclick-cluster.zip"')],
                    )

                if path == "/skill":
                    body = pages.skill_page(session.username, session.csrf_token)
                    return self._send(HTTPStatus.OK, body.encode())

                if path == "/skill.md":
                    return self._send(
                        HTTPStatus.OK,
                        pages.skill_markdown().encode(),
                        "text/markdown; charset=utf-8",
                        [("Content-Disposition", 'attachment; filename="SKILL.md"')],
                    )

                return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")

            def _deploy(self, pages: WebPages, session: Any) -> None:
                query = self._query()
                node_id = query.get("node", "")
                kind = query.get("kind", "")
                if kind in ("toml", "env"):
                    plan = pages.deploy_plan(node_id)
                    if plan is None:
                        return self._send(HTTPStatus.NOT_FOUND, "没有这个节点".encode(), "text/plain; charset=utf-8")
                    text = plan.toml if kind == "toml" else plan.env
                    name = plan.toml_name if kind == "toml" else ".env"
                    headers = [("Content-Disposition", f'attachment; filename="{name}"')] if query.get("dl") else None
                    return self._send(HTTPStatus.OK, text.encode(), "text/plain; charset=utf-8", headers)

                body = pages.deploy_page(node_id, session.username, session.csrf_token)
                if body is None:
                    return self._redirect("/config?tab=cluster")
                return self._send(HTTPStatus.OK, body.encode())

            def _json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
                body = json.dumps(payload, ensure_ascii=False, default=str).encode()
                return self._send(status, body, "application/json; charset=utf-8")

            def do_HEAD(self) -> None:
                """按 GET 的状态码与响应头作答，但不返回响应体。

                实现了 GET 就该实现 HEAD（HTTP 规范如此）。缺了它会落到标准库的
                501 兜底，于是用 HEAD 的监控探针、反向代理健康检查、链接检查器
                会把一个健康的管理端判成故障。
                """
                self._head_only = True
                try:
                    self.do_GET()
                finally:
                    self._head_only = False

            def do_POST(self) -> None:
                """处理登录及经会话和 CSRF 双重校验的状态变更。"""
                path = self.path.split("?", 1)[0].rstrip("/") or "/"

                if path == "/login":
                    return self._handle_login()

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

                pages = server.pages
                if pages is None:
                    return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")

                if path == "/test":
                    if form.get("action") == "import_curl":
                        imported, notes, error = pages.import_curl(form)
                        body = pages.test_page(
                            imported,
                            None,
                            session.username,
                            session.csrf_token,
                            curl_notes=notes,
                            curl_error=error,
                        )
                        return self._send(HTTPStatus.OK, body.encode())
                    result = pages.run_test(form)
                    return self._redirect(f"/test?r={pages.stash_test_result(form, result)}")

                if path == "/components":
                    node_id = form.get("node", "")
                    if node_id:
                        return self._redirect(f"/components?node={quote(node_id)}")
                    ok, message = pages.refresh_components()
                    log.info(f"Web 端刷新组件状态：{message}")
                    _ = ok
                    return self._redirect("/components")

                if path == "/api/components/action":
                    node_id = form.get("node", "")
                    ok, message = pages.component_action(form.get("op", ""), form.get("extra", ""), node_id)
                    return self._json(
                        {"ok": ok, "message": message, "job": pages.component_status(node_id), "node": node_id}
                    )

                if path == "/api/nodes/probe":
                    return self._json(pages.probe_node(form.get("node_id", ""), form.get("address", "")))

                if path == "/config":
                    if form.get("action") == "generate_secret":
                        token = pages.generate_secret(form.get("secret", ""))
                        return self._redirect(f"/config?g={token}" if token else "/config")
                    if form.get("action") == "add_node":
                        body = pages.add_node(form, session.username, session.csrf_token)
                        return self._send(HTTPStatus.OK, body.encode())
                    if form.get("action") == "remove_node":
                        body = pages.remove_node(form, session.username, session.csrf_token)
                        return self._send(HTTPStatus.OK, body.encode())
                    body = pages.save_config(form, session.username, session.csrf_token)
                    return self._send(HTTPStatus.OK, body.encode())

                return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")

            def _handle_login(self) -> None:
                locked_for = server.sessions.is_locked(self.source)
                if locked_for > 0:
                    body = render_login(error=f"失败次数过多，请 {locked_for:.0f} 秒后再试")
                    return self._send(HTTPStatus.TOO_MANY_REQUESTS, body.encode())

                form = self._read_form()
                username = form.get("username", "")
                password = form.get("password", "")
                # 完全没提交凭据的请求是畸形请求，不是一次"密码错误"。不区分的话，
                # 5 个空 body 的 POST（Content-Length 为 0、非数字、超 256KB 上限，
                # 或连接中途断开导致 body 读不全）就能把管理员锁在门外 300 秒——
                # 而这几种请求任何人都发得出来，不需要知道用户名。
                if not username and not password:
                    log.warning(f"Web 端收到不含凭据的登录请求，来源 {self.source}（不计入失败锁定）")
                    return self._send(
                        HTTPStatus.BAD_REQUEST, "登录请求缺少用户名或密码".encode(), "text/plain; charset=utf-8"
                    )
                if not server._credentials.verify(username, password):
                    server.sessions.record_failure(self.source)
                    return self._send(HTTPStatus.UNAUTHORIZED, render_login(error="用户名或密码错误").encode())

                server.sessions.record_success(self.source)
                session_id, _ = server.sessions.create(username)
                log.info(f"Web 端登录成功：{username}（来自 {self.source}）")
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

    def _safe_live(self) -> dict[str, Any]:
        try:
            return self._live()
        except Exception as e:
            log.warning(f"取总览实时数据失败：{e}")
            return {}


__all__ = ["COOKIE_NAME", "WebConfig", "WebServer"]
