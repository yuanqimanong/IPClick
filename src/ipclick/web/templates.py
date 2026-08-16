"""Web 端的 HTML 渲染。

手写字符串模板而不是引模板引擎：页面就几个，为此多一条依赖不值。
代价是**每一处插值都必须自己转义**——:func:`esc` 是这里最重要的函数，
节点 id、URL、错误信息、网页源码这些都来自配置或远端，一处漏转义就是 XSS。

样式与脚本在 :mod:`ipclick.web.assets`，这里只管结构。0.4 的布局是
**左导航 + 主内容 + 右状态栏**的 CSS Grid：0.3 是单栏纵向堆叠，总览页把服务器
信息、各适配器、渲染引擎、集群、最近请求全挤在一条竖线上，只能一路往下滚。
"""

from typing import Any

from ipclick.trace import TraceRecord
from ipclick.web.assets import SCRIPT_BOOT, SCRIPT_MAIN, STYLE


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


def attr(text: Any) -> str:
    """属性值转义。和 :func:`esc` 同一套规则，单独一个名字是为了在阅读时
    一眼看出"这里是属性上下文"——属性里漏转义引号能直接跳出属性、注入事件处理器。
    """
    return esc(text)


# --------------------------------------------------------------------------- #
# 导航
# --------------------------------------------------------------------------- #

#: 简笔图标。内联 SVG——页面不允许任何外部资源，图标字体和图片都不行。
_ICONS: dict[str, str] = {
    "gauge": '<path d="M2 12a10 10 0 0 1 20 0"/><path d="m12 12 4-4"/><circle cx="12" cy="12" r="1"/>',
    "activity": '<path d="M3 12h4l3 8 4-16 3 8h4"/>',
    "flask": '<path d="M9 3h6"/><path d="M10 3v6L4.5 18a2 2 0 0 0 1.7 3h11.6a2 2 0 0 0 1.7-3L14 9V3"/>'
    '<path d="M7 14h10"/>',
    "blocks": '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/>'
    '<rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    "sliders": '<path d="M4 6h16"/><path d="M4 12h16"/><path d="M4 18h16"/><circle cx="9" cy="6" r="2"/>'
    '<circle cx="15" cy="12" r="2"/><circle cx="8" cy="18" r="2"/>',
    "share": '<circle cx="6" cy="12" r="3"/><circle cx="18" cy="6" r="3"/><circle cx="18" cy="18" r="3"/>'
    '<path d="m8.6 10.6 6.8-3.2"/><path d="m8.6 13.4 6.8 3.2"/>',
}

#: 导航项：``(路径, 标题, 图标)``
NAV: tuple[tuple[str, str, str], ...] = (
    ("/", "总览", "gauge"),
    ("/trace", "请求流", "activity"),
    ("/test", "试一试", "flask"),
    ("/components", "组件", "blocks"),
    ("/config", "配置", "sliders"),
    ("/nodes", "节点", "share"),
)


def _icon(name: str) -> str:
    return f'<svg viewBox="0 0 24 24" aria-hidden="true">{_ICONS.get(name, "")}</svg>'


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


def _stat(number: Any, label: str, *, accent: bool = False) -> str:
    cls = "stat accent" if accent else "stat"
    return f'<div class="{cls}"><div class="n">{esc(number)}</div><div class="l">{esc(label)}</div></div>'


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
        f'<input type="hidden" name="csrf_token" value="{attr(csrf)}">'
        f'<input type="hidden" name="action" value="{attr(action)}">'
    )


def _messages(messages: list[str], errors: list[str]) -> str:
    return "".join(f'<div class="msg good">✓ {esc(m)}</div>' for m in messages) + "".join(
        f'<div class="msg err">{esc(e)}</div>' for e in errors
    )


def _card(title: str, body: str, *, hint: str = "", actions: str = "") -> str:
    head = f"<h2>{esc(title)}</h2>"
    if hint:
        head += f'<span class="hint">{esc(hint)}</span>'
    if actions:
        head += f"<div>{actions}</div>"
    return f'<section class="card"><div class="card-head">{head}</div>{body}</section>'


# --------------------------------------------------------------------------- #
# 登录
# --------------------------------------------------------------------------- #


def render_login(error: str | None = None) -> str:
    error_html = f'<div class="msg err" style="margin-top:1rem">{esc(error)}</div>' if error else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>IPClick 登录</title>
<style>{STYLE}</style>
<script>{SCRIPT_BOOT}</script>
</head><body>
<div class="login-wrap"><div class="login">
  <div class="brand"><span class="mark">IP</span><span class="name">IPClick 管理端</span></div>
  <form method="post" action="/login">
    <label for="u">用户名</label>
    <input id="u" name="username" autocomplete="username" autofocus required>
    <label for="p">密码</label>
    <input id="p" name="password" type="password" autocomplete="current-password" required>
    <button class="primary" type="submit" style="margin-top:1.25rem;width:100%">登录</button>
  </form>
  {error_html}
</div></div>
<script>{SCRIPT_MAIN}</script>
</body></html>"""


# --------------------------------------------------------------------------- #
# 页面骨架
# --------------------------------------------------------------------------- #


def _page(
    body: str,
    username: str,
    csrf: str,
    active: str,
    *,
    title: str,
    subtitle: str = "",
    actions: str = "",
    rail: str = "",
    version: str = "",
    job_running: bool = False,
) -> str:
    """页面外壳：左导航 + 主内容（+ 右状态栏）。"""
    nav = "".join(
        f'<a href="{attr(path)}" class="{"on" if path == active else ""}">{_icon(icon)}<span>{esc(label)}</span></a>'
        for path, label, icon in NAV
    )
    shell_class = "shell has-rail" if rail else "shell"
    rail_html = f'<aside class="rail">{rail}</aside>' if rail else ""
    subtitle_html = f'<p class="sub">{subtitle}</p>' if subtitle else ""
    actions_html = f'<div class="head-actions">{actions}</div>' if actions else ""

    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · IPClick</title>
<style>{STYLE}</style>
<script>{SCRIPT_BOOT}</script>
</head><body data-csrf="{attr(csrf)}" data-job-running="{"1" if job_running else "0"}">
<div class="{shell_class}">
  <aside class="side">
    <div class="brand">
      <span class="mark">IP</span>
      <span><span class="name">IPClick</span> <span class="ver">{esc(version)}</span></span>
    </div>
    <nav>{nav}</nav>
    <div class="spacer"></div>
    <div class="foot">
      <div class="theme" role="group" aria-label="主题">
        <button type="button" data-theme-set="auto" aria-pressed="true">跟随系统</button>
        <button type="button" data-theme-set="light" aria-pressed="false">亮</button>
        <button type="button" data-theme-set="dark" aria-pressed="false">暗</button>
      </div>
      <div class="who">已登录：{esc(username)}</div>
      <form method="post" action="/logout">
        <input type="hidden" name="csrf_token" value="{attr(csrf)}">
        <button type="submit" style="width:100%">退出登录</button>
      </form>
    </div>
  </aside>
  <main class="main">
    <div class="page-head">
      <div><h1>{esc(title)}</h1>{subtitle_html}</div>
      {actions_html}
    </div>
    {body}
  </main>
  {rail_html}
</div>
<script>{SCRIPT_MAIN}</script>
</body></html>"""


# --------------------------------------------------------------------------- #
# 总览
# --------------------------------------------------------------------------- #


def render_dashboard(snapshot: dict[str, Any], username: str, csrf: str, actions_enabled: bool) -> str:
    if "error" in snapshot:
        return _page(
            f'<div class="msg err">取状态失败：{esc(snapshot["error"])}</div>',
            username,
            csrf,
            "/",
            title="总览",
        )

    server = dict(snapshot.get("server") or {})
    security = dict(snapshot.get("security") or {})
    limits = dict(snapshot.get("limits") or {})
    browser = dict(snapshot.get("browser") or {})
    cluster = dict(snapshot.get("cluster") or {})
    stats = dict(snapshot.get("trace") or {})
    process = dict(stats.get("process") or {})
    recorder = dict(stats.get("recorder") or {})
    components = list(snapshot.get("components") or [])
    recent: list[TraceRecord] = list(snapshot.get("recent") or [])

    body = f"""
  <div data-live-src="/fragment/dashboard" data-live-interval="5000">{dashboard_live(snapshot)}</div>

  {
        _card(
            "各适配器",
            _adapter_table(process.get("by_adapter") or {}),
            hint="本次启动以来的分适配器统计",
        )
    }

  {
        _card(
            "最近请求",
            _trace_table(recent[:10]),
            actions='<a class="btn small" href="/trace">看完整请求流 →</a>',
        )
    }

  {
        _card(
            "集群",
            _cluster_summary(cluster) + _cluster_table(list(cluster.get("nodes") or []), csrf, actions_enabled),
            actions='<a class="btn small" href="/nodes">管理节点 →</a>',
        )
    }
"""

    rail = f"""
  <h2>服务端</h2>
  <table class="kv">{
        _rows(
            [
                ("监听地址", f"<code>{esc(server.get('address', '?'))}</code>"),
                ("本节点 id", f"<code>{esc(server.get('node_id', '?'))}</code>"),
                ("运行模式", esc(server.get("mode", "?"))),
                ("worker 线程", esc(server.get("max_workers", "?"))),
                ("默认适配器", f"<code>{esc(server.get('default_adapter', '?'))}</code>"),
                ("请求压缩", esc(server.get("compression", "—"))),
                ("配置文件", f"<code>{esc(server.get('config_path', '—'))}</code>"),
            ]
        )
    }</table>

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
  <p class="note" style="margin-top:.5rem">
    这几项刻意不可从网页修改。<b>机密</b>（令牌、密码、证书内容）一律不在本页显示、
    也不接受从本页写入——请改 <code>.env</code>，需要新值可以在<a href="/config">配置</a>页生成一个。
  </p>

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

  <h2>限流</h2>
  <table class="kv">{
        _rows(
            [
                ("按 host 并发上限", esc(limits.get("per_host_max_concurrent") or "不限")),
                ("按 host QPS 上限", esc(limits.get("per_host_qps") or "不限")),
                ("等待额度超时", f"{esc(limits.get('wait_timeout', '—'))} s"),
            ]
        )
    }</table>

  <h2>可选组件</h2>
  {_component_summary(components)}
  <p class="note" style="margin-top:.5rem"><a href="/components">安装 / 卸载 →</a></p>
"""

    return _page(
        body,
        username,
        csrf,
        "/",
        title="总览",
        subtitle=f"监听 <code>{esc(server.get('address', '?'))}</code>",
        version=str(server.get("version", "")),
        rail=rail,
    )


def dashboard_live(snapshot: dict[str, Any]) -> str:
    """总览页里会自己刷新的那一块。

    单独成函数是为了让 ``/fragment/dashboard`` 复用**同一段**渲染代码——
    在 JS 里另写一份就是两套逻辑，迟早对不上。
    """
    stats = dict(snapshot.get("trace") or {})
    process = dict(stats.get("process") or {})
    total = int(process.get("total", 0))
    cards = "".join(
        [
            _stat(f"{total:,}", "本次启动以来请求数"),
            _stat(f"{process.get('success_rate', 0)}%", "成功率", accent=True),
            _stat(f"{process.get('avg_ms', 0)} ms", "平均耗时"),
            _stat(process.get("in_flight", 0), f"在途（峰值 {process.get('peak_in_flight', 0)}）"),
            _stat(_bytes(process.get("bytes", 0)), "累计响应体"),
            _stat(_uptime(process.get("uptime_seconds", 0)), "运行时长"),
        ]
    )
    return f'<div class="stats">{cards}</div><div style="margin-top:1rem">{_status_bar(process)}</div>'


def _component_summary(components: list[dict[str, Any]]) -> str:
    """右栏里的组件状态速览。详细的在 /components。"""
    if not components:
        return '<p class="note">—</p>'
    rows = "".join(
        f"<tr><td><code>{esc(c.get('name'))}</code></td><td class='right'>{_component_badge(c)}</td></tr>"
        for c in components
    )
    return f'<table class="kv"><tbody>{rows}</tbody></table>'


def _component_badge(component: dict[str, Any]) -> str:
    if not component.get("package"):
        return _pill("未装", "mute")
    if component.get("browser") is False:
        # 包装了但本体没下，是最容易被误判成"能用"的状态
        return _pill("缺本体", "bad")
    if component.get("browser") is None and component.get("kind") == "browser":
        return _pill("本体未知", "warn")
    return _pill("可用", "ok")


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


_STATUS_COLORS = {
    "2xx": "var(--ok)",
    "3xx": "var(--accent)",
    "4xx": "var(--warn)",
    "5xx": "var(--bad)",
    "failure": "#8250df",
}


def _status_bar(process: dict[str, Any]) -> str:
    by_status = dict(process.get("by_status") or {})
    total = sum(int(v) for v in by_status.values())
    if not total:
        return '<p class="note">本次启动以来还没有处理过请求。</p>'
    segments = "".join(
        f'<i style="width:{int(count) / total * 100:.2f}%;background:{_STATUS_COLORS.get(name, "var(--fg-faint)")}"></i>'
        for name, count in sorted(by_status.items())
    )
    legend = "".join(
        f'<span><span class="dot" style="background:{_STATUS_COLORS.get(name, "var(--fg-faint)")};'
        f'display:inline-block;margin-right:.25rem"></span>{esc(name)} <b>{int(count):,}</b></span>'
        for name, count in sorted(by_status.items())
    )
    return f'<div class="bar">{segments}</div><div class="legend">{legend}</div>'


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


def _cluster_summary(cluster: dict[str, Any]) -> str:
    if not cluster.get("nodes"):
        return '<p class="note">未配置集群节点（单机模式）。<a href="/nodes">去添加 →</a></p>'
    forward = bool(cluster.get("forward"))
    mode = (
        _pill("服务端转发", "ok") + " 本节点收到任务后按策略分发"
        if forward
        else _pill("客户端分发", "mute") + " 本节点只执行自己收到的任务，分发由调用方负责"
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
                f'<form method="post" action="/action" class="inline-form">'
                f"{_hidden(csrf, name)}"
                f'<input type="hidden" name="node_id" value="{attr(node.get("id"))}">'
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
            f'<tr><td colspan="{9 if show_node else 8}" class="err">{esc(record.error)}</td></tr>'
            if record.error
            else ""
        )
        rows.append(
            f'<tr><td class="nowrap">{esc(record.when)}</td>'
            f"<td>{_status_pill(record.status_code)}</td>"
            f"<td>{esc(record.method)}</td>"
            f'<td><span class="url" title="{attr(record.url)}">{esc(record.url or "—")}</span></td>'
            f"<td><code>{esc(record.adapter)}</code></td>"
            f"{node_cell}"
            f'<td class="right nowrap">{record.duration_ms:,} ms</td>'
            f'<td class="right nowrap">{_bytes(record.size)}</td>'
            f"<td>{' '.join(flags)}</td></tr>{error_row}"
        )
    node_head = "<th>节点</th>" if show_node else ""
    head = (
        f"<tr><th>时间</th><th>状态</th><th>方法</th><th>URL</th><th>适配器</th>{node_head}"
        f'<th class="right">耗时</th><th class="right">大小</th><th></th></tr>'
    )
    return f'<div class="scroll"><table class="data"><thead>{head}</thead><tbody>{"".join(rows)}</tbody></table></div>'


def trace_live(
    records: list[TraceRecord],
    stats: dict[str, Any],
    *,
    source: str = "memory",
) -> str:
    """请求流里会自己刷新的那一块（指标 + 分布条 + 表格）。

    ``/fragment/trace`` 直接返回它，前端只换这一块的 innerHTML。0.3 用的是
    ``<meta refresh>`` 整页重载：每 3 秒滚动位置丢失、正在填的过滤条件被冲掉、
    页面白闪一次。渲染仍然在服务端，所以不存在"JS 里那份和 Python 这份对不上"。
    """
    process = dict(stats.get("process") or {})
    window = dict(stats.get("window") or {})
    recorder = dict(stats.get("recorder") or {})

    source_note = (
        f"数据来自 <b>SQLite</b>（共 {int(recorder.get('rows', 0)):,} 条，保留 {esc(recorder.get('retention_days'))} 天）"
        if source == "sqlite"
        else f"数据来自<b>内存缓冲</b>（最近 {esc(recorder.get('memory_size', 0))} 条，重启即丢）。"
        f'要查历史请在<a href="/config">配置</a>里打开 <code>[TRACE].sqlite_enabled</code>'
    )

    cards = "".join(
        [
            _stat(f"{int(process.get('total', 0)):,}", "本次启动"),
            _stat(process.get("in_flight", 0), "在途请求", accent=True),
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

    return f"""
  <div class="stats">{cards}</div>
  <section class="card" style="margin-top:1rem">{_status_bar(process)}</section>
  <section class="card">
    <div class="card-head"><h2>请求</h2><span class="hint">{source_note}</span></div>
    {_trace_table(records)}
  </section>
  {_top_hosts(list(stats.get("top_hosts") or []))}
  {_daily(list(stats.get("daily") or []))}
"""


def render_trace(
    records: list[TraceRecord],
    stats: dict[str, Any],
    filters: dict[str, str],
    username: str,
    csrf: str,
    *,
    source: str = "memory",
    live: bool = True,
    fragment_url: str = "/fragment/trace",
) -> str:
    """请求流页面。``live`` 打开时那一块每 3 秒自己更新，就是"实时看着请求打进来"。"""
    process = dict(stats.get("process") or {})
    adapters = sorted({r.adapter for r in records} | set((process.get("by_adapter") or {}).keys()))
    adapter_options = "".join(
        f'<option value="{attr(a)}"{" selected" if filters.get("adapter") == a else ""}>{esc(a)}</option>'
        for a in adapters
    )
    status_options = "".join(
        f'<option value="{attr(value)}"{" selected" if filters.get("status") == value else ""}>{esc(label)}</option>'
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

    live_attrs = f' data-live-src="{attr(fragment_url)}" data-live-interval="3000"' if live else ""
    body = f"""
  <section class="card">
    <form method="get" action="/trace" class="filters">
      <div><label for="f-status">状态</label><select id="f-status" name="status">{status_options}</select></div>
      <div><label for="f-adapter">适配器</label><select id="f-adapter" name="adapter">
        <option value="">全部适配器</option>{adapter_options}</select></div>
      <div><label for="f-kw">URL 包含</label><input id="f-kw" name="q" value="{attr(filters.get("q", ""))}"></div>
      <div><label for="f-limit">条数</label>
        <input id="f-limit" name="limit" value="{attr(filters.get("limit", "100"))}" style="min-width:5rem"></div>
      <div class="check">
        <input type="checkbox" id="f-live" name="live" value="1"{" checked" if live else ""}>
        <label for="f-live">实时刷新</label>
      </div>
      <div><button type="submit">应用</button></div>
      <input type="hidden" name="_" value="1">
    </form>
  </section>
  <div{live_attrs}>{trace_live(records, stats, source=source)}</div>
"""
    return _page(
        body,
        username,
        csrf,
        "/trace",
        title="请求流",
        subtitle="实时看请求打进来" + ("（每 3 秒更新）" if live else "（实时刷新已关闭）"),
    )


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
    head = (
        '<tr><th>目标 host</th><th class="right">请求</th>'
        '<th class="right">失败</th><th class="right">平均耗时</th></tr>'
    )
    table = f'<div class="scroll"><table class="data"><thead>{head}</thead><tbody>{rows}</tbody></table></div>'
    return _card("目标站点排行", table)


def _daily(daily: list[dict[str, Any]]) -> str:
    if not daily:
        return ""
    peak = max(int(d.get("total", 0)) for d in daily) or 1
    rows = "".join(
        f"<tr><td><code>{esc(d.get('day'))}</code></td>"
        f'<td style="width:55%"><div class="bar">'
        f'<i style="width:{int(d.get("ok", 0)) / peak * 100:.1f}%;background:var(--ok)"></i>'
        f'<i style="width:{int(d.get("failed", 0)) / peak * 100:.1f}%;background:var(--bad)"></i></div></td>'
        f'<td class="right">{int(d.get("total", 0)):,}</td>'
        f'<td class="right">{int(d.get("failed", 0)):,}</td>'
        f'<td class="right">{esc(d.get("avg_ms", 0))} ms</td></tr>'
        for d in daily
    )
    head = (
        '<tr><th>日期</th><th>成功 / 失败</th><th class="right">总数</th>'
        '<th class="right">失败</th><th class="right">平均耗时</th></tr>'
    )
    table = f'<div class="scroll"><table class="data"><thead>{head}</thead><tbody>{rows}</tbody></table></div>'
    return _card("按天趋势", table)


# --------------------------------------------------------------------------- #
# 试一试
# --------------------------------------------------------------------------- #


def render_test(
    form: dict[str, str],
    result: dict[str, Any] | None,
    choices: list[dict[str, Any]],
    username: str,
    csrf: str,
    *,
    nodes: list[dict[str, Any]] | None = None,
    curl_notes: list[str] | None = None,
    curl_error: str = "",
) -> str:
    """ "试一试"页面：填个 URL 就地发一次请求，看链路和源码。

    请求走的是本进程的 TaskService，和真实调用方走的是**同一条**代码路径——
    包括 SSRF 准入、限流、以及（开了转发时）分发到子节点。所以这里看到的
    行为就是线上行为，而不是另写一套只在页面上成立的逻辑。
    """
    adapter_select = _adapter_select(choices, form.get("adapter", ""))
    method_options = "".join(
        f'<option value="{attr(m)}"{" selected" if form.get("method", "GET") == m else ""}>{esc(m)}</option>'
        for m in ("GET", "POST", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS")
    )
    node_row = _target_node_row(nodes or [], form.get("target_node", ""))

    import_notes = "".join(f'<div class="msg caution">{esc(n)}</div>' for n in (curl_notes or []))
    import_error = f'<div class="msg err">{esc(curl_error)}</div>' if curl_error else ""

    body = f"""
  <section class="card">
    <div class="card-head">
      <h2>从 curl 导入</h2>
      <span class="hint">DevTools 里「复制为 cURL」，粘进来自动填表</span>
    </div>
    {import_error}{import_notes}
    <form method="post" action="/test">
      {_hidden(csrf, "import_curl")}
      <textarea name="curl" rows="3"
        placeholder="curl 'https://example.com/api' -X POST -H 'content-type: application/json' --data-raw '{{}}'"
      ></textarea>
      <div class="actions"><button type="submit">解析并填入下面的表单</button></div>
    </form>
  </section>

  <section class="card">
    <div class="card-head"><h2>请求</h2></div>
    <form method="post" action="/test">
      {_hidden(csrf, "test")}
      <div class="field-row">
        <label for="t-url">网址</label>
        <input id="t-url" name="url" placeholder="https://example.com/" required
               value="{attr(form.get("url", ""))}">
      </div>
      <div class="field-row">
        <label for="t-adapter">适配器<span class="hint">灰掉的是本机没装的，装上就能用</span></label>
        <div>{adapter_select}</div>
      </div>
      {node_row}
      <div class="field-row">
        <label for="t-method">方法</label>
        <div><select id="t-method" name="method">{method_options}</select></div>
      </div>
      <div class="field-row">
        <label for="t-timeout">超时（秒）</label>
        <div><input id="t-timeout" name="timeout" value="{attr(form.get("timeout", "30"))}"></div>
      </div>
      <div class="field-row">
        <label for="t-body">请求体<span class="hint">POST/PUT 时用；留空则不带</span></label>
        <textarea id="t-body" name="body" rows="3">{esc(form.get("body", ""))}</textarea>
      </div>
      <div class="field-row">
        <label for="t-headers">额外请求头<span class="hint">每行一个 <code>Name: value</code></span></label>
        <textarea id="t-headers" name="headers" rows="3">{esc(form.get("headers", ""))}</textarea>
      </div>
      <div class="actions">
        <button class="primary" type="submit">发送请求</button>
        <span class="note">照常受 SSRF 准入与限流约束，也会像真实请求一样出现在<a href="/trace">请求流</a>里。</span>
      </div>
    </form>
    <div class="msg tip" style="margin-top:1rem">
      <b>点一次就好，页面会等结果。</b>
      <code>curl_cffi</code> / <code>niquests</code> 通常一两秒；<code>browser</code> 要真启动一个浏览器，
      <b>冷启动首次可能几十秒</b>，之后就快了。重复点击会叠加同样多的真实请求。
      诊断路径<b>不重试</b>：这里要看的是第一次失败的真实原因。
    </div>
  </section>
  {_test_result(result)}
"""
    return _page(body, username, csrf, "/test", title="试一试", subtitle="就地发一次请求，看链路与返回的源码")


def _adapter_select(choices: list[dict[str, Any]], selected: str) -> str:
    """分组下拉框。

    两条规则和 0.3 不同：

    * **没装的也列出来**（``disabled`` + 安装命令），而不是从列表里消失。消失会
      让对着文档看的人以为文档和实现对不上，也不知道到底支持哪些。
    * ``browser`` 归到"浏览器渲染"组里并写明"自动选择引擎"，不和真实适配器名
      混排——它不是第六个可选组件，是个占位值。
    """
    groups: list[str] = []
    for group in choices:
        options: list[str] = []
        for item in group.get("items") or []:
            value = str(item.get("value", ""))
            available = bool(item.get("available"))
            hint = str(item.get("hint") or "")
            label = str(item.get("label") or value)
            if not available:
                label = f"{label} — {hint}"
            options.append(
                f'<option value="{attr(value)}"'
                f"{' selected' if value == selected else ''}"
                f"{' disabled' if not available else ''}"
                f' title="{attr(hint)}">{esc(label)}</option>'
            )
        groups.append(f'<optgroup label="{attr(group.get("title", ""))}">{"".join(options)}</optgroup>')
    return f'<select id="t-adapter" name="adapter">{"".join(groups)}</select>'


def _target_node_row(nodes: list[dict[str, Any]], selected: str) -> str:
    """ "目标节点"下拉。没配集群就整行不显示——单机部署不该看到集群相关的控件。"""
    if not nodes:
        return ""
    options = '<option value="">按策略自动选（默认）</option>' + "".join(
        f'<option value="{attr(n.get("id"))}"{" selected" if str(n.get("id")) == selected else ""}>'
        f"{esc(n.get('id'))} — {esc(n.get('address'))}{'（本机）' if n.get('is_self') else ''}</option>"
        for n in nodes
    )
    return f"""
      <div class="field-row">
        <label for="t-node">目标节点<span class="hint">强制打到这一台，跳过负载均衡。验证新加的节点用</span></label>
        <div><select id="t-node" name="target_node">{options}</select></div>
      </div>"""


def _test_result(result: dict[str, Any] | None) -> str:
    if result is None:
        return ""
    if result.get("error_only"):
        return f'<section class="card"><div class="card-head"><h2>结果</h2></div><div class="msg err">{esc(result.get("error"))}</div></section>'

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
    error = f'<div class="msg err">{esc(result.get("error"))}</div>' if result.get("error") else ""
    headers = dict(result.get("headers") or {})
    header_rows = (
        "".join(f"<tr><th>{esc(k)}</th><td><code>{esc(v)}</code></td></tr>" for k, v in sorted(headers.items()))
        or '<tr><td class="note">无</td></tr>'
    )
    truncated = (
        f'<p class="note">源码过长，只显示前 {int(result.get("shown", 0)):,} 字节'
        f"（共 {_bytes(result.get('size', 0))}）。</p>"
        if result.get("truncated")
        else ""
    )
    return f"""
  <section class="card">
    <div class="card-head"><h2>结果</h2></div>
    {error}
    <div class="grid two">
      <div><h3 class="sub-head">链路</h3>
        <table class="kv">{rows}</table></div>
      <div><h3 class="sub-head">响应头</h3>
        <div class="scroll"><table class="kv">{header_rows}</table></div></div>
    </div>
  </section>
  <section class="card">
    <div class="card-head"><h2>源码</h2></div>
    {truncated}
    <pre>{esc(result.get("body", ""))}</pre>
  </section>
"""


# --------------------------------------------------------------------------- #
# 组件
# --------------------------------------------------------------------------- #


def render_components(
    components: list[dict[str, Any]],
    username: str,
    csrf: str,
    *,
    toolchain: str,
    job: dict[str, Any] | None,
    messages: list[str],
    errors: list[str],
    bodies: dict[str, tuple[str, int]],
) -> str:
    """可选组件：安装状态 + 装 / 卸。

    0.3 里这是总览页上一张只读的「渲染引擎」表，而且**漏掉了 niquests**——它是
    纯 HTTP 适配器，不属于"渲染引擎"，于是五个 extras 里有一个完全没有展示位。
    现在按"HTTP 适配器 / 浏览器渲染"分组覆盖全部五个。
    """
    http = [c for c in components if c.get("kind") == "http"]
    browser = [c for c in components if c.get("kind") == "browser"]
    running = bool(job and job.get("status") == "running")

    body = f"""
  {_messages(messages, errors)}
  <div class="msg tip">
    <b>「Python 包」和「浏览器本体」是两件事。</b>
    <code>pip install</code> 只装前者；camoufox 的本体有 1 GB 上下，缺了它第一次请求会当场
    开始下载并超时，所以这里提前拦住而不是等它去下。<br>
    安装用的是 <code>{esc(toolchain)}</code>，绑定当前解释器，不会装到别的环境去。
  </div>

  <section class="card" id="job-box"{"" if job else " hidden"}>
    <div class="card-head"><h2>执行中的任务</h2></div>
    <div id="job-title">{_job_title(job)}</div>
    <pre class="term" id="job-output">{esc(chr(10).join(job.get("output") or [])) if job else ""}</pre>
  </section>

  {_card("HTTP 适配器", _component_cards(http, csrf, bodies, running), hint="curl_cffi 是核心依赖，随主包一起装")}
  {_card("浏览器渲染", _component_cards(browser, csrf, bodies, running), hint="一台机器通常只会用其中一个")}
"""
    actions = (
        f'<form method="post" action="/components" class="inline-form">{_hidden(csrf, "refresh")}'
        f'<button type="submit">刷新状态</button></form>'
    )
    return _page(
        body,
        username,
        csrf,
        "/components",
        title="可选组件",
        subtitle="装 / 卸 IPClick 声明的五个 extras，状态不需要重启进程就会更新",
        actions=actions,
        job_running=running,
    )


def _job_title(job: dict[str, Any] | None) -> str:
    if not job:
        return ""
    status = str(job.get("status"))
    badge = (
        '<span class="spin"></span> 执行中'
        if status == "running"
        else (_pill("成功", "ok") if status == "succeeded" else _pill("失败", "bad"))
    )
    return (
        f"{badge} <b>{esc(job.get('title'))}</b> "
        f'<span class="note">{esc(job.get("elapsed", 0))}s · {esc(job.get("command", ""))}</span>'
    )


def _component_cards(
    components: list[dict[str, Any]],
    csrf: str,
    bodies: dict[str, tuple[str, int]],
    busy: bool,
) -> str:
    if not components:
        return '<p class="note">—</p>'
    _ = csrf  # 按钮走 fetch，CSRF 从 body 的 data-csrf 上取
    return f'<div class="components">{"".join(_component_card(c, bodies, busy) for c in components)}</div>'


def _component_card(component: dict[str, Any], bodies: dict[str, tuple[str, int]], busy: bool) -> str:
    extra = str(component.get("extra", ""))
    installed = bool(component.get("package"))
    body_state = component.get("browser")
    is_browser = component.get("kind") == "browser"
    disabled = " disabled" if busy else ""

    version = f' <span class="note">{esc(component.get("version"))}</span>' if component.get("version") else ""
    package_line = f'<div><span class="k">Python 包</span>{_pill("已装", "ok") if installed else _pill("未装", "mute")}{version}</div>'

    body_line = ""
    if is_browser:
        if not installed:
            badge = _pill("—", "mute")
        elif body_state is True:
            badge = _pill("已就绪", "ok")
        elif body_state is False:
            badge = _pill("未下载", "bad")
        else:
            badge = _pill("未知", "warn")
        detail = str(component.get("detail") or "")
        hint = f' <span class="note" title="{attr(detail)}">{esc(detail[:60])}</span>' if detail else ""
        body_line = f'<div><span class="k">浏览器本体</span>{badge}{hint}</div>'

    actions: list[str] = []
    if installed:
        # 卸载要说清楚"卸的是什么"：pip uninstall 不会删掉那 1 GB 浏览器本体，
        # 不说的话用户会以为空间释放了。
        location, size = bodies.get(extra, ("", 0))
        leftover = (
            f"\\n\\n注意：只卸 Python 包。浏览器本体（{_bytes(size)}，位于 {location}）不会被删除，需要时请自行删除该目录。"
            if location and size
            else ""
        )
        actions.append(
            f'<button class="small danger" data-install="uninstall" data-extra="{attr(extra)}"'
            f' data-confirm="确定卸载 {attr(component.get("name"))} 吗？{leftover}"{disabled}>卸载</button>'
        )
    else:
        actions.append(
            f'<button class="small primary" data-install="install" data-extra="{attr(extra)}"{disabled}>安装</button>'
        )

    if is_browser and component.get("browser_command"):
        label = "重新下载浏览器本体" if body_state is True else "下载浏览器本体"
        actions.append(
            f'<button class="small" data-install="fetch" data-extra="{attr(extra)}"'
            f"{disabled if installed else ' disabled'}>{esc(label)}</button>"
        )

    ready_class = " ready" if component.get("ready") else ""
    return f"""
    <div class="comp{ready_class}">
      <div class="top">
        <span class="nm">{esc(component.get("name"))}</span>
        {_component_badge(component)}
        <code class="note">ipclick[{esc(extra)}]</code>
      </div>
      <div class="why">{esc(component.get("summary", ""))}</div>
      <div class="levels">{package_line}{body_line}</div>
      <div class="acts">{"".join(actions)}</div>
    </div>"""


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
    generators: list[dict[str, Any]] | None = None,
    generated: dict[str, Any] | None = None,
) -> str:
    sections: list[str] = []
    for title, fields in groups:
        rows = "".join(_config_row(field) for field in fields)
        sections.append(f"<fieldset><legend>{esc(title)}</legend>{rows}</fieldset>")

    body = f"""
  {_messages(messages, errors)}
  {_generated_secret(generated)}

  <section class="card">
    <div class="card-head">
      <h2>可编辑项</h2>
      <span class="hint">保存后写回 <code>{esc(config_path)}</code>（先留一份 <code>.bak</code>）</span>
    </div>
    <p class="note">文件里的注释与格式都保留，只替换被改动那一行的值。</p>
    <form method="post" action="/config">
      {_hidden(csrf, "save_config")}
      {"".join(sections)}
      <div class="actions">
        <button class="primary" type="submit">保存到 {esc(config_path)}</button>
        <span class="note">标了 <b>需重启</b> 的项，改完要重启 ipclick 才生效。</span>
      </div>
    </form>
  </section>

  {_secret_generators(generators or [], csrf)}

  <section class="card">
    <div class="card-head"><h2>只读项</h2></div>
    <p class="note">这些刻意不可从网页修改：本服务能代任意 URL 发请求，一个能从网页
       关掉内网拦截、改掉令牌的管理端，等于给自己装了个跳板。要改请编辑
       <code>{esc(config_path)}</code> 或 <code>.env</code> 后重启。</p>
    <table class="kv">{_rows(readonly_note)}</table>
  </section>
"""
    return _page(body, username, csrf, "/config", title="配置", subtitle=f"<code>{esc(config_path)}</code>")


def _generated_secret(generated: dict[str, Any] | None) -> str:
    """刚生成的机密。**只显示这一次**——取完即弃，刷新就没了。

    刻意不落盘、不写进配置：机密的正规位置是 ``.env``，由人自己粘过去。
    这也顺带保证了"不可再次查看"——服务端根本没留副本。
    """
    if not generated:
        return ""
    shared = bool(generated.get("shared"))
    warning = (
        '<div class="msg caution" style="margin-top:.75rem"><b>这是集群共享密钥：'
        "必须原样复制到<b>所有其他节点</b>的 <code>.env</code>。</b>"
        "每台机器各自生成一个的话，派生出来的节点令牌互不匹配，转发会全部 UNAUTHENTICATED。</div>"
        if shared
        else '<div class="msg tip" style="margin-top:.75rem">这是本机独有的凭据，'
        "改完重启本进程即可生效，不需要同步给其他机器。</div>"
    )
    note = (
        f'<p class="note" style="margin-top:.5rem">{esc(generated.get("note", ""))}</p>'
        if generated.get("note")
        else ""
    )
    value = str(generated.get("value", ""))
    env = str(generated.get("env", ""))
    return f"""
  <section class="card" style="border-color:var(--accent)">
    <div class="card-head">
      <h2>{esc(generated.get("label", "新凭据"))}</h2>
      <span class="hint">只显示这一次，关掉页面就再也看不到了</span>
    </div>
    <label for="gen-val">写进 <code>.env</code> 的这一行</label>
    <div style="display:flex;gap:.5rem;align-items:center">
      <input id="gen-val" class="mono" readonly value="{attr(env + "=" + value)}">
      <button type="button" data-copy="gen-val">复制</button>
    </div>
    {warning}
    {note}
    <p class="note" style="margin-top:.5rem">
      服务端<b>没有</b>保存这个值——它只在这次响应里出现过。没抄下来就再生成一个。
    </p>
  </section>"""


def _secret_generators(generators: list[dict[str, Any]], csrf: str) -> str:
    if not generators:
        return ""
    rows = "".join(
        f"<tr><td><b>{esc(g.get('label'))}</b>"
        f"{' ' + _pill('全集群一致', 'warn') if g.get('shared') else ' ' + _pill('本机独有', 'mute')}"
        f'<div class="note">{esc(g.get("note", ""))}</div></td>'
        f"<td><code>{esc(g.get('env'))}</code></td>"
        f"<td>{esc(g.get('source', ''))}</td>"
        f'<td class="right"><form method="post" action="/config" class="inline-form">'
        f"{_hidden(csrf, 'generate_secret')}"
        f'<input type="hidden" name="secret" value="{attr(g.get("env"))}">'
        f'<button class="small" type="submit">生成</button></form></td></tr>'
        for g in generators
    )
    head = '<tr><th>凭据</th><th>环境变量</th><th>当前来源</th><th class="right"></th></tr>'
    table = f'<div class="scroll"><table class="data"><thead>{head}</thead><tbody>{rows}</tbody></table></div>'
    return _card(
        "生成凭据",
        table + '<p class="note" style="margin-top:.75rem">生成的值<b>只显示一次</b>，'
        "服务端不保存、不写进任何文件——请自己粘进 <code>.env</code>。</p>",
        hint="随机生成一个足够长的值，省得自己想",
    )


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
            f'<input type="hidden" name="__present__{attr(name)}" value="1">'
            f'<input type="checkbox" id="{attr(name)}" name="{attr(name)}" value="1"{checked}>'
        )
    elif kind == "choice":
        options = "".join(
            f'<option value="{attr(choice)}"{" selected" if str(value) == str(choice) else ""}>{esc(choice)}</option>'
            for choice in field.get("choices") or ()
        )
        control = f'<select id="{attr(name)}" name="{attr(name)}">{options}</select>'
    else:
        control = f'<input id="{attr(name)}" name="{attr(name)}" value="{attr("" if value is None else value)}">'

    return f'<div class="field-row"><label for="{attr(name)}">{esc(field["label"])}{hint}</label><div>{control}</div></div>'


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
    hot_reload: bool = False,
) -> str:
    rows: list[str] = []
    for index, node in enumerate(nodes):
        is_self = str(node.get("id")) == self_id
        node_id = str(node.get("id", ""))
        rows.append(
            f"<tr data-node-row>"
            f'<td><input name="node_id_{index}" value="{attr(node_id)}"></td>'
            f'<td><input name="node_address_{index}" data-node-address value="{attr(node.get("address", ""))}"></td>'
            f'<td style="width:6rem"><input name="node_weight_{index}" value="{attr(node.get("weight", 100))}"></td>'
            f"<td>{_pill('本机', 'info') if is_self else ''}</td>"
            f'<td class="note">{esc(node.get("token_source", ""))}</td>'
            f'<td class="nowrap"><button type="button" class="small" data-probe="{attr(node_id)}">测试连接</button>'
            f'<div class="result" data-probe-result></div></td>'
            f"</tr>"
        )

    auth_note = (
        '<div class="msg good">✓ 已配置集群共享密钥，每个节点的令牌由它派生（各节点令牌互不相同）。</div>'
        if internal_auth
        else '<div class="msg err">未配置集群内部鉴权：任何能连到这些端口的人都可以借本集群发请求。'
        "请在<b>所有</b>节点的 <code>.env</code> 里放同一个 <code>IPCLICK_CLUSTER_SECRET</code>——"
        '可以在<a href="/config">配置</a>页一键生成。</div>'
    )
    reload_note = (
        "保存后<b>立即生效</b>，不需要重启：新节点马上参与转发轮询。"
        if hot_reload
        else "本进程没开服务端转发，保存只写文件；开了转发的节点保存后会立即生效。"
    )

    body = f"""
  {_messages(messages, errors)}
  {auth_note}
  <div class="msg tip">
    服务端转发：{_pill("已开启", "ok") if forward else _pill("未开启", "mute")} ·
    本节点 <code>{esc(self_id or "未识别")}</code> ·
    转发开关在<a href="/config">配置</a>页。<b>本机也要列进下面的表格</b>才会分到活。
  </div>

  <section class="card">
    <div class="card-head">
      <h2>节点列表</h2>
      <span class="hint">写回 <code>{esc(config_path)}</code> 的 <code>[CLUSTER].nodes</code></span>
    </div>
    <form method="post" action="/nodes">
      {_hidden(csrf, "save_nodes")}
      <div class="scroll"><table class="data">
        <thead><tr><th>id</th><th>地址 host:port</th><th>权重</th><th></th><th>令牌来源</th><th></th></tr></thead>
        <tbody>{"".join(rows)}
          <tr>
            <td><input name="new_node_id" placeholder="留空则用地址"></td>
            <td><input name="new_node_address" placeholder="192.168.1.101:9527"></td>
            <td><input name="new_node_weight" value="100"></td>
            <td colspan="3" class="note">新增一行</td>
          </tr>
        </tbody>
      </table></div>
      <div class="actions">
        <button class="primary" type="submit">保存节点列表</button>
        <span class="note">把某一行的地址清空 = 删除该节点。{reload_note}</span>
      </div>
    </form>
    <p class="note" style="margin-top:.75rem">
      「测试连接」只验<b>连通性</b>和<b>集群内部鉴权</b>，不发业务请求。
      失败时会区分"连不上"（查进程和网络）和"鉴权不通过"（核对各节点 <code>.env</code> 里的
      <code>IPCLICK_CLUSTER_SECRET</code>）——这两种的排查方向完全相反。
    </p>
    <p class="note">节点的 <code>token</code> 不接受从网页写入——机密只走 <code>.env</code>。
      需要给某个节点单独指定令牌时，请在配置文件里给那一项加 <code>token = "..."</code>。</p>
  </section>
"""
    return _page(body, username, csrf, "/nodes", title="集群节点", subtitle="加减机器、就地验证连通性与鉴权")


__all__ = [
    "NAV",
    "dashboard_live",
    "esc",
    "render_components",
    "render_config",
    "render_dashboard",
    "render_login",
    "render_nodes",
    "render_test",
    "render_trace",
    "trace_live",
]
