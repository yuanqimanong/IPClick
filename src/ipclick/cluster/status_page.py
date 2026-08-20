"""独立、只读的集群状态 HTML/JSON 服务。"""

from __future__ import annotations

from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import threading
from typing import Any

from typing_extensions import override

from ipclick.utils.log_util import log


_REFRESH_SECONDS = 5

_STATUS_COLORS = {
    "healthy": ("#1a7f37", "#dafbe1"),
    "unhealthy": ("#cf222e", "#ffebe9"),
    "unknown": ("#9a6700", "#fff8c5"),
}

_PAGE_TEMPLATE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="{refresh}">
<title>IPClick 集群状态</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
         margin: 2rem auto; max-width: 60rem; padding: 0 1rem; }}
  h1 {{ font-size: 1.25rem; margin: 0 0 .25rem; }}
  .sub {{ color: #656d76; margin: 0 0 1.5rem; font-size: .875rem; }}
  .cards {{ display: flex; gap: .75rem; flex-wrap: wrap; margin-bottom: 1.5rem; }}
  .card {{ border: 1px solid #d0d7de; border-radius: 6px; padding: .75rem 1rem; min-width: 6rem; }}
  .card b {{ display: block; font-size: 1.5rem; font-weight: 600; }}
  .card span {{ color: #656d76; font-size: .8125rem; }}
  .wrap {{ overflow-x: auto; }}
  table {{ border-collapse: collapse; width: 100%; min-width: 44rem; }}
  th, td {{ text-align: left; padding: .5rem .75rem; border-bottom: 1px solid #d0d7de; white-space: nowrap; }}
  th {{ font-weight: 600; color: #656d76; font-size: .8125rem; }}
  .pill {{ display: inline-block; padding: .0625rem .5rem; border-radius: 2rem; font-size: .75rem; font-weight: 600; }}
  .err {{ color: #cf222e; font-size: .8125rem; white-space: normal; }}
  footer {{ margin-top: 2rem; color: #656d76; font-size: .8125rem; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #0d1117; color: #e6edf3; }}
    .card, th, td {{ border-color: #30363d; }}
    .sub, .card span, th, footer {{ color: #8b949e; }}
  }}
</style>
<h1>IPClick 集群状态</h1>
<p class="sub">策略 <code>{strategy}</code> · 探活间隔 {probe_interval}s ·
   摘除阈值 {failure_threshold} 次失败 · 恢复阈值 {recovery_threshold} 次成功 ·
   最多故障转移 {max_failover} 次</p>

<div class="cards">
  <div class="card"><b>{total}</b><span>节点总数</span></div>
  <div class="card"><b style="color:#1a7f37">{healthy}</b><span>健康</span></div>
  <div class="card"><b style="color:#cf222e">{unhealthy}</b><span>不健康</span></div>
  <div class="card"><b style="color:#9a6700">{unknown}</b><span>未探测</span></div>
</div>

<div class="wrap">
<table>
  <thead><tr>
    <th>节点</th><th>地址</th><th>状态</th><th>权重</th>
    <th>区域</th><th>请求数</th><th>失败数</th><th>上次探活</th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table>
</div>

<footer>
  页面每 {refresh} 秒自动刷新 · JSON 接口 <a href="/api/nodes">/api/nodes</a><br>
  本页只读：增删节点与调整权重请改配置文件后重启服务端。
</footer>
"""


def _escape(text: Any) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _format_ago(seconds: float | None) -> str:
    if seconds is None:
        return "从未"
    if seconds < 60:
        return f"{int(seconds)} 秒前"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    return f"{int(seconds // 3600)} 小时前"


def render_page(snapshot: dict[str, Any]) -> str:
    """将节点池快照转义并渲染为自动刷新的状态页。"""
    rows: list[str] = []
    for node in snapshot.get("nodes", []):
        status = str(node.get("status", "unknown"))
        fg, bg = _STATUS_COLORS.get(status, _STATUS_COLORS["unknown"])
        error = node.get("last_error") or ""
        error_row = (
            f'<tr><td colspan="8" class="err">最近错误：{_escape(error)}</td></tr>'
            if error and status == "unhealthy"
            else ""
        )
        region = " / ".join(filter(None, [node.get("region", ""), node.get("zone", "")])) or "—"
        rows.append(
            f"<tr>"
            f"<td>{_escape(node.get('id'))}</td>"
            f"<td><code>{_escape(node.get('address'))}</code></td>"
            f'<td><span class="pill" style="color:{fg};background:{bg}">{_escape(status)}</span></td>'
            f"<td>{_escape(node.get('weight'))}</td>"
            f"<td>{_escape(region)}</td>"
            f"<td>{_escape(node.get('total_requests', 0))}</td>"
            f"<td>{_escape(node.get('total_failures', 0))}</td>"
            f"<td>{_escape(_format_ago(node.get('last_checked_ago')))}</td>"
            f"</tr>{error_row}"
        )

    return _PAGE_TEMPLATE.format(
        refresh=_REFRESH_SECONDS,
        strategy=_escape(snapshot.get("strategy", "?")),
        probe_interval=_escape(snapshot.get("probe_interval", "?")),
        failure_threshold=_escape(snapshot.get("failure_threshold", "?")),
        recovery_threshold=_escape(snapshot.get("recovery_threshold", "?")),
        max_failover=_escape(snapshot.get("max_failover", "?")),
        total=snapshot.get("total", 0),
        healthy=snapshot.get("healthy", 0),
        unhealthy=snapshot.get("unhealthy", 0),
        unknown=snapshot.get("unknown", 0),
        rows="".join(rows) or '<tr><td colspan="8">没有配置任何节点</td></tr>',
    )


def make_handler(snapshot_provider: Callable[[], dict[str, Any]]) -> type[BaseHTTPRequestHandler]:
    """创建绑定指定快照提供器的只读 HTTP handler。"""

    class StatusHandler(BaseHTTPRequestHandler):
        server_version: str = "IPClickStatus/1.0"

        @override
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            try:
                snapshot = snapshot_provider()
            except Exception as e:
                self._send(503, f"取集群状态失败: {e}".encode(), "text/plain; charset=utf-8")
                return

            if path == "/":
                self._send(200, render_page(snapshot).encode(), "text/html; charset=utf-8")
            elif path == "/api/nodes":
                body = json.dumps(snapshot, ensure_ascii=False, indent=2).encode()
                self._send(200, body, "application/json; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")

        def _reject_write(self) -> None:
            self._send(405, "本页只读，变更请改配置文件".encode(), "text/plain; charset=utf-8")

        def do_POST(self) -> None:
            self._reject_write()

        def do_PUT(self) -> None:
            self._reject_write()

        def do_DELETE(self) -> None:
            self._reject_write()

        def do_PATCH(self) -> None:
            self._reject_write()

    return StatusHandler


class StatusPageServer:
    """在后台线程运行轻量级集群状态 HTTP 服务。"""

    def __init__(self, snapshot_provider: Callable[[], dict[str, Any]]):
        self._provider: Callable[[], dict[str, Any]] = snapshot_provider
        self._httpd: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self, port: int, host: str = "127.0.0.1") -> bool:
        """幂等启动状态页；端口绑定失败时返回 ``False``。"""
        if self._httpd is not None:
            return True
        try:
            self._httpd = HTTPServer((host, port), make_handler(self._provider))
        except OSError as e:
            log.error(f"集群状态页启动失败 {host}:{port}: {e}")
            return False

        self._thread = threading.Thread(target=self._httpd.serve_forever, name="ipclick-status-page", daemon=True)
        self._thread.start()
        log.info(f"集群状态页: http://{host}:{port}/")
        return True

    def stop(self) -> None:
        """停止 HTTP 循环并关闭监听套接字。"""
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._thread = None


__all__ = ["StatusPageServer", "make_handler", "render_page"]
