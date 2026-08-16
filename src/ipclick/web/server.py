"""Web 管理端。

``ipclick run --web`` 起一个带登录的网页界面。只用标准库——为几个页面拉进
FastAPI 那一串依赖不划算。前端也没有构建链路：布局是纯 CSS Grid，交互是几十行
原生 JS（见 :mod:`ipclick.web.assets`）。

页面
----
* ``/`` 总览：吞吐、成功率、在途、各适配器、集群、最近请求 + 右侧常驻状态栏。
* ``/trace`` 请求流：实时看请求打进来（**局部刷新**，不重载整页），
  可按状态 / 适配器 / URL 过滤。
* ``/test`` 试一试：填个网址（或直接粘一条 curl）就地发一次请求，看链路与源码。
  走的是本进程 TaskService 的**同一条**代码路径——包括 SSRF 准入、限流、以及
  开了转发时的分发；也可以点名打到某一个节点。
* ``/components`` 组件：五个可选 extras 的安装状态与装 / 卸。
* ``/config`` 配置：白名单内的行为配置可改，**写回 ipclick.toml**（保留注释）。
* ``/nodes`` 节点：集群节点的增删改（保存即生效）+ 就地测试连接。

能改什么、不能改什么
--------------------
**能**：超时、重试、限流、日志级别、浏览器引擎、链路记录、集群策略与节点列表。

**不能**：``[SECURITY]`` 全部（令牌、TLS、SSRF 三个开关）、Web 自己的登录凭据、
集群共享密钥与各节点 token、``[BROWSER].allow_scripts``。名单在
:mod:`ipclick.web.editable`，那里也写了每一项为什么在哪一侧。

0.4 的两处放开
--------------
* **可以装依赖了。** 0.3 刻意不给这个能力（"装依赖要在机器上执行命令，那是网页
  最不该有的能力"）。现在允许，但只限 IPClick 自己声明的那五个 extras：包名走
  白名单常量、命令用列表交给 subprocess（``shell=False``），绑定当前解释器。
  见 :mod:`ipclick.web.installer`。
* **有 JavaScript 了。** 主题切换、安装任务轮询、局部刷新这三件事没有 JS 做不好。
  CSP 里用**脚本哈希**放行那两段内联脚本，而不是 ``'unsafe-inline'``——注入进来
  的 ``<script>`` 仍然执行不了。
"""

from collections.abc import Callable
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
import threading
from typing import Any, final
from urllib.parse import parse_qs

from typing_extensions import override

from ipclick.utils.log_util import log
from ipclick.web.assets import csp
from ipclick.web.auth import SessionStore, WebCredentials
from ipclick.web.pages import WebPages
from ipclick.web.templates import dashboard_live, render_dashboard, render_login


#: 会话 cookie 名
COOKIE_NAME = "ipclick_session"

#: 单次请求体上限。最大的表单是"试一试"页面（可以贴请求体和请求头）与配置页，
#: 256KB 足够宽松；不设上限的话一个大 POST 就能把内存吃掉。
MAX_BODY_BYTES = 256 * 1024

#: CSP 头。脚本哈希在进程启动时算一次——脚本是源码里的常量，不会变。
_CSP = csp()


@final
class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    """把处理请求时的异常收进本项目的日志。

    ``handle_error`` 是 :class:`socketserver.BaseServer` 的方法（**不是** handler 的），
    默认实现把完整堆栈打到 stderr —— 绕过日志配置，还带上服务端源码路径。
    最常触发它的正是"用户等不及了，关掉标签页"，而那恰恰是排查慢请求时最需要
    干净日志的时刻。
    """

    @override
    def handle_error(self, request: Any, client_address: Any) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            log.debug(f"Web 端客户端提前断开：{client_address}")
            return
        log.exception(f"Web 端处理请求出错（来自 {client_address}）：{error}")


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
        pages: WebPages | None = None,
        live_provider: Callable[[], dict[str, Any]] | None = None,
    ):
        self._provider: Callable[[], dict[str, Any]] = snapshot_provider
        self._credentials: WebCredentials = credentials
        self._actions: Callable[[str, dict[str, str]], tuple[bool, str]] | None = action_handler
        self.sessions: SessionStore = sessions or SessionStore()
        #: 请求流 / 试一试 / 配置这几页需要的数据源与写入口。为 None 时那些页面
        #: 直接返回 404——库模式下起一个只看状态的 Web 端是合法用法。
        self.pages: WebPages | None = pages
        #: 总览页自动刷新用的轻量数据源。每 5 秒被拉一次，所以不该走完整快照
        #: （那里面有 TLS 解析、集群拓扑、四个引擎的文件系统探测）。
        #: 没注入时回落到完整快照——功能一样，只是贵一点。
        self._live: Callable[[], dict[str, Any]] = live_provider or snapshot_provider
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
            self._httpd = _QuietThreadingHTTPServer((host, port), self._make_handler())
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
                # 页面里没有任何外部资源，把 CSP 收到最紧。
                # script-src 用的是两段内联脚本的 sha256，不是 'unsafe-inline'——
                # 万一某处转义漏了、注入进一行 <script>，它也执行不了。
                self.send_header("Content-Security-Policy", _CSP)
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

            def _query(self) -> dict[str, str]:
                _, _, raw = self.path.partition("?")
                return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

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

                if path == "/fragment/dashboard":
                    # 总览页里自己刷新的那一块。和整页走同一个渲染函数，
                    # 所以不存在"局部刷出来的和整页渲染的不一样"。
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
                    # 带 r= 的是 POST 之后重定向回来的，把那次结果取出来渲染
                    form, result = pages.take_test_result(self._query().get("r", ""))
                    body = pages.test_page(form, result, session.username, session.csrf_token)
                    return self._send(HTTPStatus.OK, body.encode())

                if path == "/components":
                    body = pages.components_page(session.username, session.csrf_token)
                    return self._send(HTTPStatus.OK, body.encode())

                if path == "/api/components/status":
                    return self._json({"job": pages.installer.current()})

                if path == "/config":
                    # 带 g= 的是刚生成完机密重定向回来的。取完即弃——
                    # "只显示一次"就是靠这个保证的。
                    body = pages.config_page(
                        session.username,
                        session.csrf_token,
                        generated_token=self._query().get("g", ""),
                    )
                    return self._send(HTTPStatus.OK, body.encode())

                if path == "/nodes":
                    body = pages.nodes_page(session.username, session.csrf_token)
                    return self._send(HTTPStatus.OK, body.encode())

                return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")

            def _json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
                body = json.dumps(payload, ensure_ascii=False, default=str).encode()
                return self._send(status, body, "application/json; charset=utf-8")

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

                pages = server.pages
                if pages is None:
                    return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")

                if path == "/test":
                    if form.get("action") == "import_curl":
                        # 解析出来的表单直接渲染回去，不发请求——导入和发送是
                        # 两步，中间要让人有机会看一眼、改一改。
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
                    # Post/Redirect/Get：结果存起来再 303 回去。直接渲染的话用户按
                    # F5 会把整次请求重新提交一遍——而这一页的一次提交可能是几十秒
                    # 的真实浏览器渲染，用户还以为自己"只是刷新了一下"。
                    result = pages.run_test(form)
                    return self._redirect(f"/test?r={pages.stash_test_result(form, result)}")

                if path == "/components":
                    ok, message = pages.refresh_components()
                    log.info(f"Web 端刷新组件状态：{message}")
                    _ = ok
                    return self._redirect("/components")

                if path == "/api/components/action":
                    ok, message = pages.component_action(form.get("op", ""), form.get("extra", ""))
                    return self._json({"ok": ok, "message": message, "job": pages.installer.current()})

                if path == "/api/nodes/probe":
                    return self._json(pages.probe_node(form.get("node_id", ""), form.get("address", "")))

                if path == "/config":
                    if form.get("action") == "generate_secret":
                        # Post/Redirect/Get + 一次性 token：值只在那一次 GET 里出现，
                        # 服务端不留副本，刷新就没了。
                        token = pages.generate_secret(form.get("secret", ""))
                        return self._redirect(f"/config?g={token}" if token else "/config")
                    body = pages.save_config(form, session.username, session.csrf_token)
                    return self._send(HTTPStatus.OK, body.encode())

                if path == "/nodes":
                    body = pages.save_nodes(form, session.username, session.csrf_token)
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

    def _safe_live(self) -> dict[str, Any]:
        try:
            return self._live()
        except Exception as e:
            # 自动刷新失败不该刷屏：它每 5 秒来一次，出问题就是每 5 秒一条堆栈
            log.warning(f"取总览实时数据失败：{e}")
            return {}


__all__ = ["COOKIE_NAME", "WebConfig", "WebServer"]
