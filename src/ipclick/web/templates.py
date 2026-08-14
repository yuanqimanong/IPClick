"""Web 端的 HTML 渲染。

手写字符串模板而不是引模板引擎：页面就几个，为此多一条依赖不值。
代价是**每一处插值都必须自己转义**——:func:`esc` 是这里最重要的函数，
节点 id、URL、错误信息、网页源码这些都来自配置或远端，一处漏转义就是 XSS。

页面里没有任何 JavaScript，也没有任何外部资源：自动刷新用 ``<meta refresh>``，
CSP 收到 ``default-src 'none'``。管理端后面就是一个能代发任意请求的服务，
把攻击面压到最小比页面炫酷重要。
"""

from typing import Any

from ipclick.trace import TraceRecord


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
:root { color-scheme: light dark; --line:#d0d7de; --dim:#656d76; --accent:#0969da; --bg2:#f6f8fa; }
* { box-sizing: border-box; }
body { font:14px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
       margin:0; padding:1.5rem 1rem 3rem; }
.wrap { max-width:78rem; margin:0 auto; }
h1 { font-size:1.25rem; margin:0 0 .25rem; }
h2 { font-size:1rem; margin:2rem 0 .75rem; padding-bottom:.35rem; border-bottom:1px solid var(--line); }
h3 { font-size:.875rem; margin:1.25rem 0 .5rem; color:var(--dim); }
.sub { color:var(--dim); margin:0 0 1rem; font-size:.875rem; }
table { border-collapse:collapse; width:100%; }
th,td { text-align:left; padding:.4rem .6rem; border-bottom:1px solid var(--line); vertical-align:top; }
th { font-weight:600; color:var(--dim); font-size:.8125rem; }
table.kv th { width:14rem; }
table.data th { white-space:nowrap; }
table.data td { font-size:.8125rem; }
.scroll { overflow-x:auto; }
.pill { display:inline-block; padding:.0625rem .5rem; border-radius:2rem; font-size:.75rem; font-weight:600;
        white-space:nowrap; }
.ok { color:#1a7f37; background:#dafbe1; }
.bad { color:#cf222e; background:#ffebe9; }
.warn { color:#9a6700; background:#fff8c5; }
.info { color:#0550ae; background:#ddf4ff; }
.mute { color:var(--dim); background:var(--bg2); }
.card { border:1px solid var(--line); border-radius:6px; padding:1.5rem; }
.login { max-width:22rem; margin:6rem auto; }
label { display:block; margin:.75rem 0 .25rem; font-size:.8125rem; color:var(--dim); }
label.inline { display:inline; margin:0; }
input,select,textarea { width:100%; padding:.4rem .55rem; border:1px solid var(--line); border-radius:6px;
        font:inherit; background:transparent; color:inherit; }
input[type=checkbox] { width:auto; }
textarea { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.8125rem; }
button { padding:.45rem 1rem; border:1px solid var(--line); border-radius:6px;
         font:inherit; cursor:pointer; background:transparent; color:inherit; }
button.primary { background:var(--accent); border-color:var(--accent); color:#fff; font-weight:600; }
button.small { padding:.15rem .5rem; font-size:.75rem; }
.err { color:#cf222e; font-size:.8125rem; margin-top:.75rem; }
.note { color:var(--dim); font-size:.8125rem; }
footer { margin-top:2.5rem; padding-top:1rem; border-top:1px solid var(--line);
         color:var(--dim); font-size:.8125rem; }
.topbar { display:flex; justify-content:space-between; align-items:baseline; gap:1rem; flex-wrap:wrap; }
nav { display:flex; gap:.25rem; flex-wrap:wrap; margin:1rem 0 1.5rem;
      border-bottom:1px solid var(--line); }
nav a { padding:.4rem .85rem; text-decoration:none; color:var(--dim); border-bottom:2px solid transparent;
        font-size:.875rem; }
nav a.on { color:inherit; border-bottom-color:var(--accent); font-weight:600; }
code { font-size:.85em; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
pre { background:var(--bg2); border:1px solid var(--line); border-radius:6px; padding:.75rem;
      overflow:auto; max-height:34rem; font-size:.75rem; line-height:1.5;
      font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
      /* 压缩过的 HTML 常常整个页面就一行，不折行的话只能横向拖着看 */
      white-space:pre-wrap; word-break:break-word; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(9.5rem,1fr)); gap:.75rem; margin:1rem 0; }
.stat { border:1px solid var(--line); border-radius:6px; padding:.75rem .9rem; }
.stat .n { font-size:1.5rem; font-weight:600; line-height:1.2; }
.stat .l { color:var(--dim); font-size:.75rem; margin-top:.15rem; }
.bar { display:flex; height:.4rem; border-radius:2rem; overflow:hidden; background:var(--bg2); margin:.35rem 0; }
.bar i { display:block; }
.grid2 { display:grid; grid-template-columns:repeat(auto-fit,minmax(20rem,1fr)); gap:0 2rem; }
.filters { display:flex; gap:.5rem; align-items:flex-end; flex-wrap:wrap; margin-bottom:1rem; }
.filters > div { flex:0 0 auto; }
.filters label { margin-top:0; }
.filters input,.filters select { width:auto; min-width:8rem; }
.url { max-width:30rem; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; display:inline-block; }
.right { text-align:right; }
fieldset { border:1px solid var(--line); border-radius:6px; padding:.5rem 1rem 1rem; margin:0 0 1.25rem; }
legend { font-size:.8125rem; font-weight:600; padding:0 .35rem; }
.row { display:grid; grid-template-columns:15rem 1fr; gap:.75rem; align-items:center;
       padding:.4rem 0; border-bottom:1px solid var(--line); }
.row:last-child { border-bottom:none; }
.row > label { margin:0; color:inherit; font-size:.875rem; }
.row .hint { display:block; color:var(--dim); font-size:.75rem; margin-top:.1rem; }
.actions { display:flex; gap:.75rem; align-items:center; margin-top:1rem; }
@media (prefers-color-scheme: dark) {
  body { background:#0d1117; color:#e6edf3; }
  :root { --line:#30363d; --dim:#8b949e; --accent:#1f6feb; --bg2:#161b22; }
  .ok { color:#3fb950; background:#0f2f18; }
  .bad { color:#f85149; background:#3c1618; }
  .warn { color:#d29922; background:#3a2d10; }
  .info { color:#58a6ff; background:#0c2d6b; }
}
"""

#: 导航项：``(路径, 标题)``
NAV: tuple[tuple[str, str], ...] = (
    ("/", "总览"),
    ("/trace", "请求流"),
    ("/test", "试一试"),
    ("/config", "配置"),
    ("/nodes", "节点"),
)


# --------------------------------------------------------------------------- #
# 小组件
# --------------------------------------------------------------------------- #


def _rows(pairs: list[tuple[str, Any]]) -> str:
    return "".join(f"<tr><th>{esc(k)}</th><td>{v}</td></tr>" for k, v in pairs)


def _pill(text: str, kind: str) -> str:
    return f'<span class="pill {kind}">{esc(text)}</span>'


def _bool_pill(value: Any, *, good_is_true: bool = True) -> str:
    truthy = bool(value)
    good = truthy if good_is_true else not truthy
    return _pill("是" if truthy else "否", "ok" if good else "warn")


def _status_pill(status_code: int) -> str:
    if status_code < 0:
        return _pill("失败", "bad")
    if status_code < 300:
        return _pill(str(status_code), "ok")
    if status_code < 400:
        return _pill(str(status_code), "info")
    if status_code < 500:
        return _pill(str(status_code), "warn")
    return _pill(str(status_code), "bad")


def _stat(number: Any, label: str) -> str:
    return f'<div class="stat"><div class="n">{esc(number)}</div><div class="l">{esc(label)}</div></div>'


def _bytes(size: Any) -> str:
    try:
        value = float(size)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _hidden(csrf: str, action: str) -> str:
    return (
        f'<input type="hidden" name="csrf_token" value="{esc(csrf)}">'
        f'<input type="hidden" name="action" value="{esc(action)}">'
    )


# --------------------------------------------------------------------------- #
# 登录
# --------------------------------------------------------------------------- #


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
    <button class="primary" type="submit" style="margin-top:1.25rem">登录</button>
  </form>
  {error_html}
</div></div>"""


# --------------------------------------------------------------------------- #
# 页面骨架
# --------------------------------------------------------------------------- #


def _page(body: str, username: str, csrf: str, active: str, *, refresh: int = 0, flash: str = "") -> str:
    nav = "".join(
        f'<a href="{esc(path)}" class="{"on" if path == active else ""}">{esc(title)}</a>' for path, title in NAV
    )
    # 自动刷新用 meta 而不是 JS：CSP 里没有 script-src，页面一行 JS 都不该有
    meta_refresh = f'<meta http-equiv="refresh" content="{refresh}">' if refresh > 0 else ""
    refresh_note = f" · 每 {refresh} 秒自动刷新" if refresh > 0 else ""
    flash_html = f'<p class="note" style="margin-bottom:1rem">{flash}</p>' if flash else ""
    return f"""<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{meta_refresh}
<title>IPClick 管理端</title><style>{_STYLE}</style>
<div class="wrap">
  <div class="topbar">
    <div>
      <h1>IPClick 管理端</h1>
      <p class="sub">已登录：{esc(username)}{esc(refresh_note)}</p>
    </div>
    <form method="post" action="/logout">
      <input type="hidden" name="csrf_token" value="{esc(csrf)}">
      <button type="submit">退出登录</button>
    </form>
  </div>
  <nav>{nav}</nav>
  {flash_html}
  {body}
  <footer>
    机密（令牌、密码、证书内容）一律不在本页显示，也不接受从本页写入——请改 <code>.env</code>。<br>
    JSON 接口：<a href="/api/status">/api/status</a> · <a href="/api/trace">/api/trace</a>
  </footer>
</div>"""


# --------------------------------------------------------------------------- #
# 总览
# --------------------------------------------------------------------------- #


def render_dashboard(snapshot: dict[str, Any], username: str, csrf: str, actions_enabled: bool) -> str:
    if "error" in snapshot:
        return _page(f'<p class="err">取状态失败：{esc(snapshot["error"])}</p>', username, csrf, "/")

    server = dict(snapshot.get("server") or {})
    security = dict(snapshot.get("security") or {})
    limits = dict(snapshot.get("limits") or {})
    browser = dict(snapshot.get("browser") or {})
    cluster = dict(snapshot.get("cluster") or {})
    stats = dict(snapshot.get("trace") or {})
    process = dict(stats.get("process") or {})
    recorder = dict(stats.get("recorder") or {})
    recent: list[TraceRecord] = list(snapshot.get("recent") or [])

    total = int(process.get("total", 0))
    cards = "".join(
        [
            _stat(f"{total:,}", "本次启动以来请求数"),
            _stat(f"{process.get('success_rate', 0)}%", "成功率"),
            _stat(f"{process.get('avg_ms', 0)} ms", "平均耗时"),
            _stat(process.get("in_flight", 0), f"在途（峰值 {process.get('peak_in_flight', 0)}）"),
            _stat(_bytes(process.get("bytes", 0)), "累计响应体"),
            _stat(_uptime(process.get("uptime_seconds", 0)), "运行时长"),
        ]
    )

    body = f"""
  <div class="cards">{cards}</div>
  {_status_bar(process)}
  <div class="grid2">
    <div>
      <h2>服务端</h2>
      <table class="kv">{
        _rows(
            [
                ("监听地址", f"<code>{esc(server.get('address', '?'))}</code>"),
                ("版本", esc(server.get("version", "?"))),
                ("本节点 id", f"<code>{esc(server.get('node_id', '?'))}</code>"),
                ("worker 线程", esc(server.get("max_workers", "?"))),
                ("默认适配器", f"<code>{esc(server.get('default_adapter', '?'))}</code>"),
                ("配置文件", f"<code>{esc(server.get('config_path', '—'))}</code>"),
            ]
        )
    }</table>
    </div>
    <div>
      <h2>安全</h2>
      <table class="kv">{
        _rows(
            [
                ("传输层", esc(security.get("tls", "?"))),
                ("令牌鉴权", _bool_pill(security.get("auth"))),
                ("拦截内网地址", _bool_pill(security.get("block_private_networks"))),
                ("拦截元数据端点", _bool_pill(security.get("block_metadata_endpoints"))),
                ("允许页内 JS", _bool_pill(browser.get("allow_scripts"), good_is_true=False)),
                ("集群内部鉴权", _bool_pill(cluster.get("internal_auth"))),
            ]
        )
    }</table>
      <p class="note">这几项刻意不可从网页修改——见页脚。</p>
    </div>
  </div>

  <div class="grid2">
    <div>
      <h2>链路记录</h2>
      <table class="kv">{
        _rows(
            [
                (
                    "数据来源",
                    _pill(
                        "SQLite" if recorder.get("source") == "sqlite" else "仅内存",
                        "ok" if recorder.get("source") == "sqlite" else "mute",
                    ),
                ),
                ("内存缓冲", f"{esc(recorder.get('in_memory', 0))} / {esc(recorder.get('memory_size', 0))} 条"),
                ("落盘记录数", f"{int(recorder.get('rows', 0)):,}" if recorder.get("sqlite_enabled") else "—"),
                (
                    "数据文件",
                    (
                        f"<code>{esc(recorder.get('sqlite_path'))}</code>（{_bytes(recorder.get('db_bytes', 0))}）"
                        if recorder.get("sqlite_enabled")
                        else "未启用"
                    ),
                ),
                ("丢弃条数", _dropped(recorder)),
                ("保留天数", esc(recorder.get("retention_days", "—"))),
            ]
        )
    }</table>
    </div>
    <div>
      <h2>限流与压缩</h2>
      <table class="kv">{
        _rows(
            [
                ("按 host 并发上限", esc(limits.get("per_host_max_concurrent") or "不限")),
                ("按 host QPS 上限", esc(limits.get("per_host_qps") or "不限")),
                ("等待额度超时", f"{esc(limits.get('wait_timeout', '—'))} s"),
                ("请求压缩", esc(server.get("compression", "—"))),
            ]
        )
    }</table>
    </div>
  </div>

  <h2>各适配器</h2>
  {_adapter_table(process.get("by_adapter") or {})}

  <h2>渲染引擎</h2>
  {_engine_table(list(snapshot.get("engines") or []), browser)}

  <h2>集群</h2>
  {_cluster_summary(cluster)}
  {_cluster_table(list(cluster.get("nodes") or []), csrf, actions_enabled)}

  <h2>最近请求</h2>
  {_trace_table(recent[:12])}
  <p class="note"><a href="/trace">看完整请求流 →</a></p>
"""
    return _page(body, username, csrf, "/", refresh=15)


def _uptime(seconds: Any) -> str:
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "—"
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    if total < 86400:
        return f"{total // 3600}h{total % 3600 // 60}m"
    return f"{total // 86400}d{total % 86400 // 3600}h"


def _dropped(recorder: dict[str, Any]) -> str:
    dropped = int(recorder.get("dropped", 0) or 0)
    if not recorder.get("sqlite_enabled"):
        return "—"
    if dropped == 0:
        return _pill("0", "ok")
    # 丢弃必须显眼：静默丢链路记录会让"没有记录"和"没发生过"混为一谈
    return _pill(f"{dropped:,}（写盘跟不上）", "bad")


_STATUS_COLORS = {"2xx": "#1a7f37", "3xx": "#0969da", "4xx": "#bf8700", "5xx": "#cf222e", "failure": "#8250df"}


def _status_bar(process: dict[str, Any]) -> str:
    by_status = dict(process.get("by_status") or {})
    total = sum(int(v) for v in by_status.values())
    if not total:
        return '<p class="note">本次启动以来还没有处理过请求。</p>'
    segments = "".join(
        f'<i style="width:{int(count) / total * 100:.2f}%;background:{_STATUS_COLORS.get(name, "#8b949e")}"></i>'
        for name, count in sorted(by_status.items())
    )
    legend = " · ".join(
        f'<span style="color:{_STATUS_COLORS.get(name, "#8b949e")}">■</span> {esc(name)} {int(count):,}'
        for name, count in sorted(by_status.items())
    )
    return f'<div class="bar">{segments}</div><p class="note">{legend}</p>'


def _adapter_table(by_adapter: dict[str, Any]) -> str:
    if not by_adapter:
        return '<p class="note">暂无数据。</p>'
    rows = "".join(
        f"<tr><td><code>{esc(name)}</code></td>"
        f'<td class="right">{int(data.get("total", 0)):,}</td>'
        f'<td class="right">{int(data.get("ok", 0)):,}</td>'
        f'<td class="right">{int(data.get("failed", 0)):,}</td>'
        f'<td class="right">{esc(data.get("avg_ms", 0))} ms</td>'
        f'<td class="right">{_bytes(data.get("bytes", 0))}</td></tr>'
        for name, data in by_adapter.items()
    )
    head = (
        '<tr><th>适配器</th><th class="right">请求</th><th class="right">成功</th>'
        '<th class="right">失败</th><th class="right">平均耗时</th><th class="right">流量</th></tr>'
    )
    return f'<div class="scroll"><table class="data"><thead>{head}</thead><tbody>{rows}</tbody></table></div>'


def _engine_table(engines: list[dict[str, Any]], browser: dict[str, Any]) -> str:
    if not engines:
        return '<p class="note">浏览器渲染已关闭。</p>'
    active = str(browser.get("engine") or "")
    rows: list[str] = []
    for engine in engines:
        name = str(engine.get("name", ""))
        package = bool(engine.get("package"))
        body_ready = engine.get("browser")
        current = _pill("当前", "info") if name == active else ""

        pkg_badge = _pill("已装", "ok") if package else _pill("未装", "mute")
        if not package:
            body_badge = _pill("—", "mute")
        elif body_ready is True:
            body_badge = _pill("已就绪", "ok")
        elif body_ready is False:
            # 这一格是重点：包装了但本体没下，是最容易被误判成"能用"的状态
            body_badge = _pill("未下载", "bad")
        else:
            body_badge = _pill("未知", "warn")

        hint = "" if engine.get("available") else f"<code>{esc(engine.get('install', ''))}</code>"
        rows.append(
            f"<tr><td><code>{esc(name)}</code> {current}</td><td>{pkg_badge}</td><td>{body_badge}</td>"
            f'<td class="note">{esc(engine.get("detail", ""))}</td><td>{hint}</td></tr>'
        )
    note = (
        '<p class="note">本页<b>不安装</b>任何东西——装依赖要在机器上执行命令，'
        "那是网页最不该有的能力。上面给的是安装命令，复制到那台机器上跑。<br>"
        "「Python 包」和「浏览器本体」是两件事：<code>pip install</code> 只装前者。"
        "camoufox 的本体有 1 GB 上下，缺了它第一次请求会当场开始下载并超时，"
        "所以这里提前拦住而不是等它去下。</p>"
    )
    head = "<tr><th>引擎</th><th>Python 包</th><th>浏览器本体</th><th>路径 / 原因</th><th>安装命令</th></tr>"
    return f'<div class="scroll"><table class="data"><thead>{head}</thead><tbody>{"".join(rows)}</tbody></table></div>{note}'


def _cluster_summary(cluster: dict[str, Any]) -> str:
    if not cluster.get("nodes"):
        return '<p class="note">未配置集群节点（单机模式）。<a href="/nodes">去添加 →</a></p>'
    forward = bool(cluster.get("forward"))
    mode = (
        _pill("服务端转发", "ok") + "：本节点收到任务后按策略分发"
        if forward
        else _pill("客户端分发", "mute") + "：本节点只执行自己收到的任务，分发由调用方负责"
    )
    extra = ""
    if forward:
        extra = (
            f'<p class="note">本节点 <code>{esc(cluster.get("self_id") or "未识别")}</code>'
            f"{'（在轮询中）' if cluster.get('self_in_pool') else '（不参与轮询，只转发）'} · "
            f"已转发 {int(cluster.get('forwarded_requests', 0)):,} · "
            f"本地执行 {int(cluster.get('local_requests', 0)):,}</p>"
        )
    return f'<p class="note">{mode} · 策略 <code>{esc(cluster.get("strategy", "—"))}</code></p>{extra}'


def _cluster_table(nodes: list[dict[str, Any]], csrf: str, actions_enabled: bool) -> str:
    if not nodes:
        return ""

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
                f"{_hidden(csrf, name)}"
                f'<input type="hidden" name="node_id" value="{esc(node.get("id"))}">'
                f'<button class="small" type="submit">{text}</button></form>'
            )

        error = node.get("last_error") or ""
        error_row = (
            f'<tr><td colspan="7" class="err">最近错误：{esc(error)}</td></tr>'
            if error and status == "unhealthy"
            else ""
        )
        rows.append(
            f"<tr><td>{esc(node.get('id'))}{' ' + _pill('本机', 'info') if node.get('is_self') else ''}</td>"
            f"<td><code>{esc(node.get('address'))}</code></td>"
            f"<td>{_pill(label, kind)}</td>"
            f'<td class="right">{esc(node.get("weight", 100))}</td>'
            f'<td class="right">{esc(node.get("total_requests", 0))}</td>'
            f'<td class="right">{esc(node.get("total_failures", 0))}</td>'
            f"<td>{action}</td></tr>{error_row}"
        )

    head = (
        '<tr><th>节点</th><th>地址</th><th>状态</th><th class="right">权重</th>'
        '<th class="right">请求</th><th class="right">失败</th><th></th></tr>'
    )
    return f'<div class="scroll"><table class="data"><thead>{head}</thead><tbody>{"".join(rows)}</tbody></table></div>'


# --------------------------------------------------------------------------- #
# 请求流
# --------------------------------------------------------------------------- #


def _trace_table(records: list[TraceRecord], *, show_node: bool = True) -> str:
    if not records:
        return '<p class="note">还没有记录。发一个请求，或用<a href="/test">试一试</a>页面造一个。</p>'
    rows: list[str] = []
    for record in records:
        flags: list[str] = []
        if record.forwarded:
            flags.append(_pill("转发", "info"))
        if record.stream:
            flags.append(_pill("流式", "mute"))
        if record.attempts > 1:
            flags.append(_pill(f"重试 {record.attempts - 1}", "warn"))
        if record.queued_ms > 0:
            flags.append(_pill(f"排队 {record.queued_ms}ms", "mute"))
        node_cell = f"<td><code>{esc(record.node_id)}</code></td>" if show_node else ""
        error_row = (
            f'<tr><td colspan="{8 if show_node else 7}" class="err">{esc(record.error)}</td></tr>'
            if record.error
            else ""
        )
        rows.append(
            f"<tr><td>{esc(record.when)}</td>"
            f"<td>{_status_pill(record.status_code)}</td>"
            f"<td>{esc(record.method)}</td>"
            f'<td><span class="url" title="{esc(record.url)}">{esc(record.url or "—")}</span></td>'
            f"<td><code>{esc(record.adapter)}</code></td>"
            f"{node_cell}"
            f'<td class="right">{record.duration_ms:,} ms</td>'
            f'<td class="right">{_bytes(record.size)}</td>'
            f"<td>{' '.join(flags)}</td></tr>{error_row}"
        )
    node_head = "<th>节点</th>" if show_node else ""
    head = (
        f"<tr><th>时间</th><th>状态</th><th>方法</th><th>URL</th><th>适配器</th>{node_head}"
        f'<th class="right">耗时</th><th class="right">大小</th><th></th></tr>'
    )
    return f'<div class="scroll"><table class="data"><thead>{head}</thead><tbody>{"".join(rows)}</tbody></table></div>'


def render_trace(
    records: list[TraceRecord],
    stats: dict[str, Any],
    filters: dict[str, str],
    username: str,
    csrf: str,
    *,
    source: str = "memory",
    live: bool = True,
) -> str:
    """请求流页面。``live`` 打开时页面自动刷新，就是"实时看着请求打进来"。"""
    process = dict(stats.get("process") or {})
    window = dict(stats.get("window") or {})
    recorder = dict(stats.get("recorder") or {})

    adapters = sorted({r.adapter for r in records} | set((process.get("by_adapter") or {}).keys()))
    adapter_options = "".join(
        f'<option value="{esc(a)}"{" selected" if filters.get("adapter") == a else ""}>{esc(a)}</option>'
        for a in adapters
    )
    status_options = "".join(
        f'<option value="{esc(value)}"{" selected" if filters.get("status") == value else ""}>{esc(label)}</option>'
        for value, label in (
            ("", "全部状态"),
            ("failed", "只看失败"),
            ("2xx", "2xx"),
            ("3xx", "3xx"),
            ("4xx", "4xx"),
            ("5xx", "5xx"),
            ("failure", "连接失败"),
        )
    )
    live_on = "1" if live else ""

    source_note = (
        f"数据来自 <b>SQLite</b>（共 {int(recorder.get('rows', 0)):,} 条，保留 {esc(recorder.get('retention_days'))} 天）"
        if source == "sqlite"
        else f"数据来自<b>内存缓冲</b>（最近 {esc(recorder.get('memory_size', 0))} 条，重启即丢）。"
        f'要查历史请在<a href="/config">配置</a>里打开 <code>[TRACE].sqlite_enabled</code>'
    )

    cards = "".join(
        [
            _stat(f"{int(process.get('total', 0)):,}", "本次启动"),
            _stat(process.get("in_flight", 0), "在途请求"),
            _stat(f"{process.get('success_rate', 0)}%", "成功率（本次启动）"),
            *(
                [
                    _stat(f"{int(window.get('total', 0)):,}", f"近 {esc(stats.get('window_days', 30))} 天"),
                    _stat(f"{window.get('success_rate', 0)}%", "成功率（同期）"),
                ]
                if window
                else []
            ),
        ]
    )

    body = f"""
  <div class="cards">{cards}</div>
  {_status_bar(process)}
  <form method="get" action="/trace" class="filters">
    <div><label for="f-status">状态</label><select id="f-status" name="status">{status_options}</select></div>
    <div><label for="f-adapter">适配器</label><select id="f-adapter" name="adapter">
      <option value="">全部适配器</option>{adapter_options}</select></div>
    <div><label for="f-kw">URL 包含</label><input id="f-kw" name="q" value="{esc(filters.get("q", ""))}"></div>
    <div><label for="f-limit">条数</label><input id="f-limit" name="limit" value="{esc(filters.get("limit", "100"))}"
         style="min-width:5rem"></div>
    <div><label class="inline"><input type="checkbox" name="live" value="1"{" checked" if live else ""}>
         实时刷新</label></div>
    <div><button type="submit">应用</button></div>
    <input type="hidden" name="_" value="{esc(live_on)}">
  </form>
  <p class="note">{source_note}</p>
  {_trace_table(records)}
  {_top_hosts(list(stats.get("top_hosts") or []))}
  {_daily(list(stats.get("daily") or []))}
"""
    # 3 秒刷新：这就是"实时看着请求打进来"。刻意不用 WebSocket——
    # 那需要 JS，而这个页面的 CSP 一行脚本都不允许。
    return _page(body, username, csrf, "/trace", refresh=3 if live else 0)


def _top_hosts(hosts: list[dict[str, Any]]) -> str:
    if not hosts:
        return ""
    rows = "".join(
        f"<tr><td><code>{esc(h.get('host'))}</code></td>"
        f'<td class="right">{int(h.get("total", 0)):,}</td>'
        f'<td class="right">{int(h.get("failed", 0)):,}</td>'
        f'<td class="right">{esc(h.get("avg_ms", 0))} ms</td></tr>'
        for h in hosts
    )
    head = '<tr><th>目标 host</th><th class="right">请求</th><th class="right">失败</th><th class="right">平均耗时</th></tr>'
    return f'<h2>目标站点排行</h2><div class="scroll"><table class="data"><thead>{head}</thead><tbody>{rows}</tbody></table></div>'


def _daily(daily: list[dict[str, Any]]) -> str:
    if not daily:
        return ""
    peak = max(int(d.get("total", 0)) for d in daily) or 1
    rows = "".join(
        f"<tr><td><code>{esc(d.get('day'))}</code></td>"
        f'<td style="width:60%"><div class="bar">'
        f'<i style="width:{int(d.get("ok", 0)) / peak * 100:.1f}%;background:#1a7f37"></i>'
        f'<i style="width:{int(d.get("failed", 0)) / peak * 100:.1f}%;background:#cf222e"></i></div></td>'
        f'<td class="right">{int(d.get("total", 0)):,}</td>'
        f'<td class="right">{int(d.get("failed", 0)):,}</td>'
        f'<td class="right">{esc(d.get("avg_ms", 0))} ms</td></tr>'
        for d in daily
    )
    head = '<tr><th>日期</th><th>成功 / 失败</th><th class="right">总数</th><th class="right">失败</th><th class="right">平均耗时</th></tr>'
    return f'<h2>按天趋势</h2><div class="scroll"><table class="data"><thead>{head}</thead><tbody>{rows}</tbody></table></div>'


# --------------------------------------------------------------------------- #
# 试一试
# --------------------------------------------------------------------------- #


def render_test(
    form: dict[str, str],
    result: dict[str, Any] | None,
    adapters: list[str],
    username: str,
    csrf: str,
) -> str:
    """ "试一试"页面：填个 URL 就地发一次请求，看链路和源码。

    请求走的是本进程的 TaskService，和真实调用方走的是**同一条**代码路径——
    包括 SSRF 准入、限流、以及（开了转发时）分发到子节点。所以这里看到的
    行为就是线上行为，而不是另写一套只在页面上成立的逻辑。
    """
    adapter_options = "".join(
        f'<option value="{esc(a)}"{" selected" if form.get("adapter") == a else ""}>{esc(a)}</option>' for a in adapters
    )
    method_options = "".join(
        f'<option value="{esc(m)}"{" selected" if form.get("method", "GET") == m else ""}>{esc(m)}</option>'
        for m in ("GET", "POST", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS")
    )

    body = f"""
  <h2>试一试</h2>
  <p class="sub">填一个网址，就地发一次请求，看链路信息与返回的源码。</p>
  <form method="post" action="/test">
    {_hidden(csrf, "test")}
    <div class="row">
      <label for="t-url">网址</label>
      <input id="t-url" name="url" placeholder="https://example.com/" required
             value="{esc(form.get("url", ""))}">
    </div>
    <div class="row">
      <label for="t-adapter">适配器<span class="hint">browser 用真实浏览器渲染后取 DOM</span></label>
      <select id="t-adapter" name="adapter">{adapter_options}</select>
    </div>
    <div class="row">
      <label for="t-method">方法</label>
      <select id="t-method" name="method">{method_options}</select>
    </div>
    <div class="row">
      <label for="t-timeout">超时（秒）</label>
      <input id="t-timeout" name="timeout" value="{esc(form.get("timeout", "30"))}">
    </div>
    <div class="row">
      <label for="t-body">请求体<span class="hint">POST/PUT 时用；留空则不带</span></label>
      <textarea id="t-body" name="body" rows="3">{esc(form.get("body", ""))}</textarea>
    </div>
    <div class="row">
      <label for="t-headers">额外请求头<span class="hint">每行一个 <code>Name: value</code></span></label>
      <textarea id="t-headers" name="headers" rows="3">{esc(form.get("headers", ""))}</textarea>
    </div>
    <div class="actions">
      <button class="primary" type="submit">发送请求</button>
      <span class="note">请求会照常受 SSRF 准入与限流约束，也会像真实请求一样出现在<a href="/trace">请求流</a>里。</span>
    </div>
    <p class="note">
      <b>点一次就好，页面会等结果</b>——这一页是同步的，没有转圈动画（页面里没有 JavaScript）。
      <code>curl_cffi</code> / <code>niquests</code> 通常一两秒；<code>browser</code> 要真启动一个浏览器，
      <b>冷启动首次可能几十秒</b>，之后就快了。重复点击会叠加同样多的真实请求。
      诊断路径**不重试**：这里要看的是第一次失败的真实原因。
    </p>
  </form>
  {_test_result(result)}
"""
    return _page(body, username, csrf, "/test")


def _test_result(result: dict[str, Any] | None) -> str:
    if result is None:
        return ""
    if result.get("error_only"):
        return f'<h2>结果</h2><p class="err">{esc(result.get("error"))}</p>'

    trace = dict(result.get("trace") or {})
    rows = _rows(
        [
            ("状态码", _status_pill(int(result.get("status_code", -1)))),
            ("实际 URL", f"<code>{esc(result.get('effective_url', ''))}</code>"),
            ("耗时", f"{int(result.get('elapsed_ms', 0)):,} ms"),
            ("响应体大小", _bytes(result.get("size", 0))),
            ("执行节点", f"<code>{esc(trace.get('node_id') or '—')}</code>"),
            ("实际适配器", f"<code>{esc(trace.get('adapter') or '—')}</code>"),
            ("尝试次数", esc(trace.get("attempts", 1))),
            ("经由转发", _bool_pill(trace.get("forwarded"), good_is_true=False)),
            ("限流排队", f"{esc(trace.get('queued_ms', 0))} ms"),
        ]
    )
    error = f'<p class="err">{esc(result.get("error"))}</p>' if result.get("error") else ""
    headers = dict(result.get("headers") or {})
    header_rows = (
        "".join(f"<tr><th>{esc(k)}</th><td><code>{esc(v)}</code></td></tr>" for k, v in sorted(headers.items()))
        or '<tr><td class="note">无</td></tr>'
    )
    truncated = (
        f'<p class="note">源码过长，只显示前 {int(result.get("shown", 0)):,} 字节（共 {_bytes(result.get("size", 0))}）。</p>'
        if result.get("truncated")
        else ""
    )
    return f"""
  <h2>结果</h2>
  {error}
  <div class="grid2">
    <div><h3>链路</h3><table class="kv">{rows}</table></div>
    <div><h3>响应头</h3><div class="scroll"><table class="kv">{header_rows}</table></div></div>
  </div>
  <h3>源码</h3>
  {truncated}
  <pre>{esc(result.get("body", ""))}</pre>
"""


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


def render_config(
    groups: list[tuple[str, list[dict[str, Any]]]],
    username: str,
    csrf: str,
    *,
    config_path: str,
    messages: list[str],
    errors: list[str],
    readonly_note: list[tuple[str, Any]],
) -> str:
    sections: list[str] = []
    for title, fields in groups:
        rows: list[str] = []
        for field in fields:
            rows.append(_config_row(field))
        sections.append(f"<fieldset><legend>{esc(title)}</legend>{''.join(rows)}</fieldset>")

    message_html = "".join(f'<p class="note">✓ {esc(m)}</p>' for m in messages)
    error_html = "".join(f'<p class="err">{esc(e)}</p>' for e in errors)

    body = f"""
  <h2>配置</h2>
  <p class="sub">保存后写回 <code>{esc(config_path)}</code>（会先留一份 <code>.bak</code>）。
     文件里的注释与格式都保留，只替换被改动那一行的值。</p>
  {message_html}{error_html}
  <form method="post" action="/config">
    {_hidden(csrf, "save_config")}
    {"".join(sections)}
    <div class="actions">
      <button class="primary" type="submit">保存到 {esc(config_path)}</button>
      <span class="note">标了 <b>需重启</b> 的项，改完要重启 ipclick 才生效。</span>
    </div>
  </form>

  <h2>只读项</h2>
  <p class="note">这些刻意不可从网页修改：本服务能代任意 URL 发请求，一个能从网页
     关掉内网拦截、改掉令牌的管理端，等于给自己装了个跳板。要改请编辑
     <code>{esc(config_path)}</code> 或 <code>.env</code> 后重启。</p>
  <table class="kv">{_rows(readonly_note)}</table>
"""
    return _page(body, username, csrf, "/config")


def _config_row(field: dict[str, Any]) -> str:
    name = str(field["name"])
    kind = str(field["kind"])
    value = field.get("value")
    hint_parts: list[str] = []
    if field.get("hint"):
        hint_parts.append(str(field["hint"]))
    if field.get("restart"):
        hint_parts.append("<b>需重启</b>")
    hint = f'<span class="hint">{" · ".join(hint_parts)}</span>' if hint_parts else ""

    if kind == "bool":
        checked = " checked" if bool(value) else ""
        control = (
            f'<input type="hidden" name="__present__{esc(name)}" value="1">'
            f'<input type="checkbox" id="{esc(name)}" name="{esc(name)}" value="1"{checked}>'
        )
    elif kind == "choice":
        options = "".join(
            f'<option value="{esc(choice)}"{" selected" if str(value) == str(choice) else ""}>{esc(choice)}</option>'
            for choice in field.get("choices") or ()
        )
        control = f'<select id="{esc(name)}" name="{esc(name)}">{options}</select>'
    else:
        control = f'<input id="{esc(name)}" name="{esc(name)}" value="{esc("" if value is None else value)}">'

    return f'<div class="row"><label for="{esc(name)}">{esc(field["label"])}{hint}</label><div>{control}</div></div>'


# --------------------------------------------------------------------------- #
# 节点
# --------------------------------------------------------------------------- #


def render_nodes(
    nodes: list[dict[str, Any]],
    username: str,
    csrf: str,
    *,
    config_path: str,
    self_id: str,
    forward: bool,
    internal_auth: bool,
    messages: list[str],
    errors: list[str],
) -> str:
    rows: list[str] = []
    for index, node in enumerate(nodes):
        is_self = str(node.get("id")) == self_id
        rows.append(
            f"<tr>"
            f'<td><input name="node_id_{index}" value="{esc(node.get("id", ""))}"></td>'
            f'<td><input name="node_address_{index}" value="{esc(node.get("address", ""))}"></td>'
            f'<td style="width:6rem"><input name="node_weight_{index}" value="{esc(node.get("weight", 100))}"></td>'
            f"<td>{_pill('本机', 'info') if is_self else ''}</td>"
            f"<td>{esc(node.get('token_source', ''))}</td>"
            f"</tr>"
        )

    message_html = "".join(f'<p class="note">✓ {esc(m)}</p>' for m in messages)
    error_html = "".join(f'<p class="err">{esc(e)}</p>' for e in errors)
    auth_note = (
        '<p class="note">✓ 已配置集群共享密钥，每个节点的令牌由它派生（各节点令牌互不相同）。</p>'
        if internal_auth
        else '<p class="err">未配置集群内部鉴权：任何能连到这些端口的人都可以借本集群发请求。'
        "请在<b>所有</b>节点的 <code>.env</code> 里放同一个 <code>IPCLICK_CLUSTER_SECRET</code>。</p>"
    )

    body = f"""
  <h2>集群节点</h2>
  <p class="sub">写回 <code>{esc(config_path)}</code> 的 <code>[CLUSTER].nodes</code>。
     改完需要重启才生效。</p>
  {message_html}{error_html}
  {auth_note}
  <p class="note">服务端转发：{_pill("已开启", "ok") if forward else _pill("未开启", "mute")} ·
     本节点 <code>{esc(self_id or "未识别")}</code> ·
     转发开关在<a href="/config">配置</a>页。
     <b>本机也要列进下面的表格</b>才会分到活。</p>
  <form method="post" action="/nodes">
    {_hidden(csrf, "save_nodes")}
    <div class="scroll"><table class="data">
      <thead><tr><th>id</th><th>地址 host:port</th><th>权重</th><th></th><th>令牌来源</th></tr></thead>
      <tbody>{"".join(rows) or ""}
        <tr>
          <td><input name="new_node_id" placeholder="留空则用地址"></td>
          <td><input name="new_node_address" placeholder="192.168.1.101:9527"></td>
          <td><input name="new_node_weight" value="100"></td>
          <td colspan="2" class="note">新增一行</td>
        </tr>
      </tbody>
    </table></div>
    <div class="actions">
      <button class="primary" type="submit">保存节点列表</button>
      <span class="note">把某一行的地址清空 = 删除该节点。</span>
    </div>
  </form>
  <p class="note">节点的 <code>token</code> 不接受从网页写入——机密只走 <code>.env</code>。
     需要给某个节点单独指定令牌时，请在配置文件里给那一项加 <code>token = "..."</code>。</p>
"""
    return _page(body, username, csrf, "/nodes")


__all__ = [
    "NAV",
    "esc",
    "render_config",
    "render_dashboard",
    "render_login",
    "render_nodes",
    "render_test",
    "render_trace",
]
