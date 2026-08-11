"""Web 端的 HTML 渲染。

手写字符串模板而不是引模板引擎：页面就两个，为此多一条依赖不值。
代价是**每一处插值都必须自己转义**——:func:`esc` 是这里最重要的函数，
节点 id、错误信息这些都可能来自配置或远端。
"""

from typing import Any


def esc(text: Any) -> str:
    """HTML 转义。所有插值都必须过这一道。"""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


_STYLE = """
:root { color-scheme: light dark; --line:#d0d7de; --dim:#656d76; }
* { box-sizing: border-box; }
body { font:14px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
       margin:0; padding:2rem 1rem; }
.wrap { max-width:64rem; margin:0 auto; }
h1 { font-size:1.25rem; margin:0 0 .25rem; }
h2 { font-size:1rem; margin:2rem 0 .75rem; padding-bottom:.35rem; border-bottom:1px solid var(--line); }
.sub { color:var(--dim); margin:0 0 1.5rem; font-size:.875rem; }
table { border-collapse:collapse; width:100%; }
th,td { text-align:left; padding:.45rem .7rem; border-bottom:1px solid var(--line); vertical-align:top; }
th { font-weight:600; color:var(--dim); font-size:.8125rem; width:14rem; }
.scroll { overflow-x:auto; }
.pill { display:inline-block; padding:.0625rem .5rem; border-radius:2rem; font-size:.75rem; font-weight:600; }
.ok { color:#1a7f37; background:#dafbe1; }
.bad { color:#cf222e; background:#ffebe9; }
.warn { color:#9a6700; background:#fff8c5; }
.card { border:1px solid var(--line); border-radius:6px; padding:1.5rem; }
.login { max-width:22rem; margin:6rem auto; }
label { display:block; margin:.75rem 0 .25rem; font-size:.8125rem; color:var(--dim); }
input { width:100%; padding:.5rem .6rem; border:1px solid var(--line); border-radius:6px;
        font:inherit; background:transparent; color:inherit; }
button { margin-top:1.25rem; padding:.5rem 1rem; border:1px solid var(--line); border-radius:6px;
         font:inherit; cursor:pointer; background:transparent; color:inherit; }
button.small { margin:0; padding:.15rem .5rem; font-size:.75rem; }
.err { color:#cf222e; font-size:.8125rem; margin-top:.75rem; }
.note { color:var(--dim); font-size:.8125rem; }
footer { margin-top:2.5rem; color:var(--dim); font-size:.8125rem; }
.topbar { display:flex; justify-content:space-between; align-items:baseline; gap:1rem; }
code { font-size:.85em; }
@media (prefers-color-scheme: dark) {
  body { background:#0d1117; color:#e6edf3; }
  :root { --line:#30363d; --dim:#8b949e; }
  .ok { color:#3fb950; background:#0f2f18; }
  .bad { color:#f85149; background:#3c1618; }
  .warn { color:#d29922; background:#3a2d10; }
}
"""


def render_login(error: str | None = None) -> str:
    error_html = f'<p class="err">{esc(error)}</p>' if error else ""
    return f"""<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>IPClick 登录</title><style>{_STYLE}</style>
<div class="wrap"><div class="card login">
  <h1>IPClick</h1>
  <p class="sub">管理端登录</p>
  <form method="post" action="/login">
    <label for="u">用户名</label>
    <input id="u" name="username" autocomplete="username" autofocus required>
    <label for="p">密码</label>
    <input id="p" name="password" type="password" autocomplete="current-password" required>
    <button type="submit">登录</button>
  </form>
  {error_html}
</div></div>"""


def _rows(pairs: list[tuple[str, Any]]) -> str:
    return "".join(f"<tr><th>{esc(k)}</th><td>{v}</td></tr>" for k, v in pairs)


def _pill(text: str, kind: str) -> str:
    return f'<span class="pill {kind}">{esc(text)}</span>'


def _bool_pill(value: Any, *, good_is_true: bool = True) -> str:
    truthy = bool(value)
    good = truthy if good_is_true else not truthy
    return _pill("是" if truthy else "否", "ok" if good else "warn")


def _cluster_table(nodes: list[dict[str, Any]], csrf: str, actions_enabled: bool) -> str:
    if not nodes:
        return '<p class="note">未配置集群节点（单机模式）。</p>'

    kinds = {"healthy": "ok", "unhealthy": "bad", "unknown": "warn"}
    rows: list[str] = []
    for node in nodes:
        status = str(node.get("status", "unknown"))
        drained = bool(node.get("drained"))
        label = "已手动摘除" if drained else status
        kind = "warn" if drained else kinds.get(status, "warn")

        action = ""
        if actions_enabled:
            name = "undrain" if drained else "drain"
            text = "恢复" if drained else "摘除"
            action = (
                f'<form method="post" action="/action" style="display:inline">'
                f'<input type="hidden" name="csrf_token" value="{esc(csrf)}">'
                f'<input type="hidden" name="action" value="{name}">'
                f'<input type="hidden" name="node_id" value="{esc(node.get("id"))}">'
                f'<button class="small" type="submit">{text}</button></form>'
            )

        error = node.get("last_error") or ""
        error_row = (
            f'<tr><td colspan="6" class="err">最近错误：{esc(error)}</td></tr>'
            if error and status == "unhealthy"
            else ""
        )
        rows.append(
            f"<tr><td>{esc(node.get('id'))}</td>"
            f"<td><code>{esc(node.get('address'))}</code></td>"
            f"<td>{_pill(label, kind)}</td>"
            f"<td>{esc(node.get('total_requests', 0))}</td>"
            f"<td>{esc(node.get('total_failures', 0))}</td>"
            f"<td>{action}</td></tr>{error_row}"
        )

    head = "<tr><th>节点</th><th>地址</th><th>状态</th><th>请求数</th><th>失败数</th><th></th></tr>"
    return f'<div class="scroll"><table><thead>{head}</thead><tbody>{"".join(rows)}</tbody></table></div>'


def render_dashboard(snapshot: dict[str, Any], username: str, csrf: str, actions_enabled: bool) -> str:
    if "error" in snapshot:
        body = f'<p class="err">取状态失败：{esc(snapshot["error"])}</p>'
        return _page(body, username, csrf)

    server = dict(snapshot.get("server") or {})
    security = dict(snapshot.get("security") or {})
    limits = dict(snapshot.get("limits") or {})
    browser = dict(snapshot.get("browser") or {})
    cluster = dict(snapshot.get("cluster") or {})

    server_rows = _rows(
        [
            ("监听地址", f"<code>{esc(server.get('address', '?'))}</code>"),
            ("版本", esc(server.get("version", "?"))),
            ("运行模式", esc(server.get("mode", "?"))),
            ("worker 线程", esc(server.get("max_workers", "?"))),
            ("默认适配器", f"<code>{esc(server.get('default_adapter', '?'))}</code>"),
            ("可用适配器", ", ".join(f"<code>{esc(a)}</code>" for a in server.get("adapters", [])) or "—"),
        ]
    )

    security_rows = _rows(
        [
            ("传输层", esc(security.get("tls", "?"))),
            ("令牌鉴权", _bool_pill(security.get("auth"))),
            ("拦截内网地址", _bool_pill(security.get("block_private_networks"))),
            ("拦截元数据端点", _bool_pill(security.get("block_metadata_endpoints"))),
            ("允许页内 JS", _bool_pill(browser.get("allow_scripts"), good_is_true=False)),
        ]
    )

    limit_rows = _rows(
        [
            ("按 host 并发上限", esc(limits.get("per_host_max_concurrent") or "不限")),
            ("按 host QPS 上限", esc(limits.get("per_host_qps") or "不限")),
            ("限流后端", f"<code>{esc(limits.get('backend', 'memory'))}</code>"),
            ("浏览器引擎", f"<code>{esc(browser.get('engine', '—'))}</code>"),
            ("浏览器页面上限", esc(browser.get("max_pages", "—"))),
        ]
    )

    body = f"""
  <h2>服务端</h2><table>{server_rows}</table>
  <h2>安全</h2><table>{security_rows}</table>
  <h2>限流与渲染</h2><table>{limit_rows}</table>
  <h2>集群节点</h2>
  {_cluster_table(list(cluster.get("nodes") or []), csrf, actions_enabled)}
"""
    return _page(body, username, csrf)


def _page(body: str, username: str, csrf: str) -> str:
    return f"""<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="15">
<title>IPClick 管理端</title><style>{_STYLE}</style>
<div class="wrap">
  <div class="topbar">
    <div>
      <h1>IPClick 管理端</h1>
      <p class="sub">已登录：{esc(username)} · 页面每 15 秒自动刷新</p>
    </div>
    <form method="post" action="/logout">
      <input type="hidden" name="csrf_token" value="{esc(csrf)}">
      <button type="submit">退出登录</button>
    </form>
  </div>
  {body}
  <footer>
    本页<b>不能修改配置</b>——改配置请编辑配置文件后重启。<br>
    节点的摘除 / 恢复只影响当前进程的运行时状态，重启即复原。<br>
    JSON 接口：<a href="/api/status">/api/status</a>
  </footer>
</div>"""


__all__ = ["esc", "render_dashboard", "render_login"]
