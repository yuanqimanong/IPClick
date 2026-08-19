from typing import Any

from ipclick.ports import DEFAULT_GRPC_PORT
from ipclick.trace import TraceRecord
from ipclick.web.assets import SCRIPT_BOOT, SCRIPT_MAIN, STYLE


TEST_RETRIES_MAX_HINT = 5

DEFAULT_GRPC_PORT_HINT = DEFAULT_GRPC_PORT


def _concurrency_shape(server: dict[str, Any]) -> str:
    processes = int(server.get("processes", 1) or 1)
    parts: list[str] = []
    parts.append(f"{processes} 进程" if processes > 1 else "单进程")
    parts.append("异步（实验性）" if server.get("async_mode") else "一请求一线程")
    text = esc(" · ".join(parts))
    if processes > 1:
        text += f'<br><span class="muted">链路记录每进程一份，本页只统计 0 号进程——总量约为实际的 1/{processes}</span>'
    return text


def esc(text: Any) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def attr(text: Any) -> str:
    return esc(text)


_ICONS: dict[str, str] = {
    "gauge": '<path d="M2 12a10 10 0 0 1 20 0"/><path d="m12 12 4-4"/><circle cx="12" cy="12" r="1"/>',
    "activity": '<path d="M3 12h4l3 8 4-16 3 8h4"/>',
    "flask": '<path d="M9 3h6"/><path d="M10 3v6L4.5 18a2 2 0 0 0 1.7 3h11.6a2 2 0 0 0 1.7-3L14 9V3"/>'
    '<path d="M7 14h10"/>',
    "blocks": '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/>'
    '<rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    "sliders": '<path d="M4 6h16"/><path d="M4 12h16"/><path d="M4 18h16"/><circle cx="9" cy="6" r="2"/>'
    '<circle cx="15" cy="12" r="2"/><circle cx="8" cy="18" r="2"/>',
    "sparkles": '<path d="M12 3v4"/><path d="M12 17v4"/><path d="M3 12h4"/><path d="M17 12h4"/>'
    '<path d="m6.3 6.3 2.4 2.4"/><path d="m15.3 15.3 2.4 2.4"/><path d="m17.7 6.3-2.4 2.4"/>'
    '<path d="m8.7 15.3-2.4 2.4"/>',
    "share": '<circle cx="6" cy="12" r="3"/><circle cx="18" cy="6" r="3"/><circle cx="18" cy="18" r="3"/>'
    '<path d="m8.6 10.6 6.8-3.2"/><path d="m8.6 13.4 6.8 3.2"/>',
}

NAV: tuple[tuple[str, str, str], ...] = (
    ("/", "总览", "gauge"),
    ("/trace", "请求流", "activity"),
    ("/test", "试一试", "flask"),
    ("/components", "组件", "blocks"),
    ("/config", "配置", "sliders"),
    ("/skill", "AI 接入", "sparkles"),
)


def _icon(name: str) -> str:
    return f'<svg viewBox="0 0 24 24" aria-hidden="true">{_ICONS.get(name, "")}</svg>'


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


_default_theme = "light"


def set_default_theme(theme: str) -> None:
    global _default_theme
    _default_theme = "dark" if theme == "dark" else "light"


def _theme_attr(theme: str | None) -> str:
    resolved = theme if theme in ("light", "dark") else _default_theme
    return f' data-default-theme="{attr(resolved)}"'


def render_login(error: str | None = None, *, theme: str | None = None) -> str:
    error_html = f'<div class="msg err" style="margin-top:1rem">{esc(error)}</div>' if error else ""
    return f"""<!doctype html>
<html lang="zh-CN"{_theme_attr(theme)}><head>
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
    theme: str | None = None,
) -> str:
    nav = "".join(
        f'<a href="{attr(path)}" class="{"on" if path == active else ""}">{_icon(icon)}<span>{esc(label)}</span></a>'
        for path, label, icon in NAV
    )
    shell_class = "shell has-rail" if rail else "shell"
    rail_html = f'<aside class="rail">{rail}</aside>' if rail else ""
    subtitle_html = f'<p class="sub">{subtitle}</p>' if subtitle else ""
    actions_html = f'<div class="head-actions">{actions}</div>' if actions else ""

    return f"""<!doctype html>
<html lang="zh-CN"{_theme_attr(theme)}><head>
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
        <button type="button" data-theme-set="light" aria-pressed="true">亮</button>
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
                ("gRPC 监听", f"<code>{esc(server.get('grpc_address') or server.get('address', '?'))}</code>"),
                (
                    "Web 管理端",
                    f"<code>{esc(server.get('web_address'))}</code>" if server.get("web_address") else "未启用",
                ),
                ("本节点 id", f"<code>{esc(server.get('node_id', '?'))}</code>"),
                ("运行模式", esc(server.get("mode", "?"))),
                ("worker 线程", esc(server.get("max_workers", "?"))),
                ("并发形态", _concurrency_shape(server)),
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
        subtitle=(
            f"gRPC <code>{esc(server.get('grpc_address') or server.get('address', '?'))}</code>"
            + (f" · Web <code>{esc(server.get('web_address'))}</code>" if server.get("web_address") else "")
        ),
        version=str(server.get("version", "")),
        rail=rail,
    )


def dashboard_live(snapshot: dict[str, Any]) -> str:
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


LIVE_INTERVALS: tuple[tuple[int, str, str], ...] = (
    (0, "关闭", "实时刷新已关闭"),
    (1000, "1 秒", "每秒更新"),
    (5000, "5 秒", "每 5 秒更新"),
    (30000, "30 秒", "每 30 秒更新"),
)

DEFAULT_LIVE_MS = 5000


def live_label(ms: int) -> str:
    for value, _, text in LIVE_INTERVALS:
        if value == ms:
            return text
    return f"每 {ms // 1000} 秒更新"


def _live_control(current_ms: int) -> str:
    buttons = "".join(
        f'<input type="radio" id="live-{ms}" name="live" value="{ms}"'
        f"{' checked' if ms == current_ms else ''}>"
        f'<label for="live-{ms}">{esc(text)}</label>'
        for ms, text, _ in LIVE_INTERVALS
    )
    return (
        '<div><label id="live-seg-label">实时刷新</label>'
        f'<div class="seg" id="live-seg" role="radiogroup" aria-labelledby="live-seg-label">{buttons}</div></div>'
    )


def _live_status(current_ms: int) -> str:
    paused = " paused" if not current_ms else ""
    return (
        f'<div class="livebar{paused}" id="live-bar">'
        f'<span class="livedot" aria-hidden="true"></span>'
        f'<span id="live-status">{esc(live_label(current_ms))}</span></div>'
    )


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
            f'<tr><td class="nowrap"><time datetime="{attr(record.iso)}">{esc(record.when)}</time></td>'
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
    live_ms: int = DEFAULT_LIVE_MS,
    fragment_url: str = "/fragment/trace",
) -> str:
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

    live_attrs = f' data-live-src="{attr(fragment_url)}" data-live-interval="{live_ms}"'
    body = f"""
  <section class="card">
    <form method="get" action="/trace" class="filters">
      <div><label for="f-status">状态</label><select id="f-status" name="status">{status_options}</select></div>
      <div><label for="f-adapter">适配器</label><select id="f-adapter" name="adapter">
        <option value="">全部适配器</option>{adapter_options}</select></div>
      <div><label for="f-kw">URL 包含</label><input id="f-kw" name="q" value="{attr(filters.get("q", ""))}"></div>
      <div><label for="f-limit">条数</label>
        <input id="f-limit" name="limit" value="{attr(filters.get("limit", "100"))}" style="min-width:5rem"></div>
      {_live_control(live_ms)}
      <div><button type="submit">应用</button></div>
      <input type="hidden" name="_" value="1">
    </form>
    {_live_status(live_ms)}
  </section>
  <div{live_attrs}>{trace_live(records, stats, source=source)}</div>
"""
    return _page(
        body,
        username,
        csrf,
        "/trace",
        title="请求流",
        subtitle=f"实时看请求打进来（{live_label(live_ms)}）",
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
    return _card("按天趋势", table, hint="按服务端本地时区分天")


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
    allow_scripts: bool = False,
) -> str:
    adapter_select = _adapter_select(choices, form.get("adapter", ""))
    method_options = "".join(
        f'<option value="{attr(m)}"{" selected" if form.get("method", "GET") == m else ""}>{esc(m)}</option>'
        for m in ("GET", "POST", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS")
    )
    node_row = _target_node_row(nodes or [], form.get("target_node", ""))
    advanced = _test_advanced(form, allow_scripts=allow_scripts)

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
        <div>
          <textarea id="t-body" name="body" rows="3">{esc(form.get("body", ""))}</textarea>
          <div class="inline-choice">{_body_kind_radios(form.get("body_kind", "raw"))}</div>
        </div>
      </div>
      <div class="field-row">
        <label for="t-headers">额外请求头<span class="hint">每行一个 <code>Name: value</code></span></label>
        <textarea id="t-headers" name="headers" rows="3">{esc(form.get("headers", ""))}</textarea>
      </div>
      {advanced}
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


IMPERSONATE_SUGGESTIONS: tuple[str, ...] = (
    "chrome",
    "chrome124",
    "chrome131",
    "chrome136",
    "safari180",
    "safari180_ios",
    "edge101",
    "firefox133",
)


def _checkbox(name: str, label: str, checked: bool, hint: str = "") -> str:
    hint_html = f'<span class="hint">{hint}</span>' if hint else ""
    return (
        f'<label class="check-inline"><input type="hidden" name="__present__{attr(name)}" value="1">'
        f'<input type="checkbox" name="{attr(name)}"{" checked" if checked else ""}>'
        f"<span>{esc(label)}</span>{hint_html}</label>"
    )


def _body_kind_radios(selected: str) -> str:
    options = (("raw", "原样发送（data）"), ("json", "作为 JSON 发送（json）"))
    return "".join(
        f'<label class="check-inline"><input type="radio" name="body_kind" value="{attr(value)}"'
        f"{' checked' if (selected or 'raw') == value else ''}><span>{esc(label)}</span></label>"
        for value, label in options
    )


def _test_advanced(form: dict[str, str], *, allow_scripts: bool) -> str:
    advanced_keys = (
        "cookies",
        "params",
        "proxy_mode",
        "proxy_url",
        "impersonate",
        "max_retries",
        "retry_backoff",
        "allowed_status_codes",
        "automation_config",
        "automation_script",
    )
    touched = any((form.get(k) or "").strip() and form.get(k) != "none" for k in advanced_keys)
    touched = touched or (form and (form.get("verify") != "on" or form.get("allow_redirects") != "on"))

    proxy_mode = form.get("proxy_mode") or "none"
    proxy_options = "".join(
        f'<option value="{attr(value)}"{" selected" if proxy_mode == value else ""}>{esc(label)}</option>'
        for value, label in (
            ("none", "不走代理"),
            ("config", "用配置文件里的 [PROXY]"),
            ("custom", "自定义（下面填）"),
        )
    )
    suggestions = "".join(f'<option value="{attr(v)}">' for v in IMPERSONATE_SUGGESTIONS)

    script_row = (
        f"""
      <div class="field-row">
        <label for="t-script">页内脚本<span class="hint">浏览器渲染专属。返回值走
          <code>x-ipclick-script-result</code> 响应头</span></label>
        <textarea id="t-script" name="automation_script" rows="3"
          placeholder="return document.title">{esc(form.get("automation_script", ""))}</textarea>
      </div>"""
        if allow_scripts
        else """
      <div class="field-row">
        <label>页内脚本</label>
        <div class="note">服务端未开启（<code>[BROWSER].allow_scripts = false</code>）。
          它等于允许调用方在服务端的浏览器里跑任意 JS，只能改配置文件打开。</div>
      </div>"""
    )

    return f"""
      <details class="more"{" open" if touched else ""}>
        <summary>更多参数<span class="hint">与 SDK 的 request() 一一对应</span></summary>
      <div class="field-row">
        <label for="t-params">查询参数<span class="hint">每行一个 <code>k=v</code>，会拼到 URL 后面</span></label>
        <textarea id="t-params" name="params" rows="2">{esc(form.get("params", ""))}</textarea>
      </div>
      <div class="field-row">
        <label for="t-cookies">Cookie<span class="hint">每行一个 <code>k=v</code></span></label>
        <textarea id="t-cookies" name="cookies" rows="2">{esc(form.get("cookies", ""))}</textarea>
      </div>
      <div class="field-row">
        <label for="t-proxy-mode">代理</label>
        <div>
          <select id="t-proxy-mode" name="proxy_mode">{proxy_options}</select>
          <input name="proxy_url" style="margin-top:.375rem"
                 placeholder="http://user:pass@host:8080（选「自定义」时填）"
                 value="{attr(form.get("proxy_url", ""))}">
          <span class="hint">选「用配置文件里的 [PROXY]」等价于 SDK 的 <code>proxy=True</code>；
            账号密码取自 .env，不会回显。</span>
        </div>
      </div>
      <div class="field-row">
        <label for="t-imp">浏览器指纹<span class="hint">仅 <code>curl_cffi</code>；留空按 chrome 处理</span></label>
        <div>
          <input id="t-imp" name="impersonate" list="imp-list" value="{attr(form.get("impersonate", ""))}"
                 placeholder="chrome">
          <datalist id="imp-list">{suggestions}</datalist>
        </div>
      </div>
      <div class="field-row">
        <label for="t-retries">重试<span class="hint">默认 0：诊断要看的是<b>第一次</b>失败的真实原因</span></label>
        <div class="two-up">
          <input id="t-retries" name="max_retries" value="{attr(form.get("max_retries", "0"))}"
                 placeholder="次数（0-{TEST_RETRIES_MAX_HINT}）">
          <input name="retry_backoff" value="{attr(form.get("retry_backoff", ""))}" placeholder="退避基数（秒）">
        </div>
      </div>
      <div class="field-row">
        <label for="t-codes">允许的状态码<span class="hint">这些不算失败、不触发重试。留空用服务端默认</span></label>
        <input id="t-codes" name="allowed_status_codes" placeholder="200, 404"
               value="{attr(form.get("allowed_status_codes", ""))}">
      </div>
      <div class="field-row">
        <label>开关</label>
        <div class="check-row">
          {_checkbox("verify", "校验目标站点证书", form.get("verify", "on") == "on")}
          {_checkbox("allow_redirects", "跟随重定向", form.get("allow_redirects", "on") == "on")}
        </div>
      </div>
      <div class="field-row">
        <label for="t-auto">自动化配置<span class="hint">浏览器渲染专属，JSON。如
          <code>{{"wait_for_selector": "#app", "screenshot": true}}</code></span></label>
        <textarea id="t-auto" name="automation_config" rows="2">{esc(form.get("automation_config", ""))}</textarea>
      </div>
      {script_row}
      <p class="note">刻意没有 <code>stream</code>：这一页同步等结果再整页渲染，
        流式在这里没有任何可观察的差别，放个开关只会让人以为验证过了。</p>
      </details>"""


def _target_node_row(nodes: list[dict[str, Any]], selected: str) -> str:
    if not nodes:
        return ""
    forwarding = bool(nodes[0].get("forwarding"))
    default_label = "按策略自动选（默认）" if forwarding else "本机执行（默认）"
    options = f'<option value="">{esc(default_label)}</option>' + "".join(
        f'<option value="{attr(n.get("id"))}"{" selected" if str(n.get("id")) == selected else ""}>'
        f"{esc(n.get('id'))} — {esc(n.get('address'))}{'（本机）' if n.get('is_self') else ''}</option>"
        for n in nodes
    )
    hint = (
        "强制打到这一台，跳过负载均衡。验证新加的节点用"
        if forwarding
        else "本机未开服务端转发，选中某一台时由本页<b>直连</b>它发一次请求（用集群内部令牌）"
    )
    return f"""
      <div class="field-row">
        <label for="t-node">目标节点<span class="hint">{hint}</span></label>
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
    registry_dir: str = "~/.cache/ms-playwright",
    nodes: list[dict[str, Any]] | None = None,
    active_node: str = "",
    remote: bool = False,
) -> str:
    http = [c for c in components if c.get("kind") == "http"]
    browser = [c for c in components if c.get("kind") == "browser"]
    running = bool(job and job.get("status") == "running")

    shared = [c for c in browser if c.get("extra") in ("playwright", "patchright")]
    revisions = (
        "".join(
            f"<tr><td><code>{esc(c.get('name'))}</code></td>"
            f"<td><code>{esc(bodies.get(str(c.get('extra')), ('', 0))[0] or '—（未下载）')}</code></td>"
            f"<td class='nowrap'>{_bytes(size) if (size := bodies.get(str(c.get('extra')), ('', 0))[1]) else '—'}</td></tr>"
            for c in shared
        )
        or '<tr><td colspan="3" class="note">两个都没装</td></tr>'
    )

    body = f"""
  {_messages(messages, errors)}
  {_component_target(nodes or [], active_node)}

  <div class="msg tip">
    <b>「Python 包」和「浏览器本体」是两件事。</b>
    <code>pip install</code> 只装前者；camoufox 的本体有 1 GB 上下，缺了它第一次请求会当场
    开始下载并超时，所以这里提前拦住而不是等它去下。<br>
    {
        "装到的是 <b>" + esc(active_node) + "</b> 那台机器上（由它自己执行，主控只是把请求转过去）。"
        if remote
        else "安装用的是 <code>" + esc(toolchain) + "</code>，绑定当前解释器，不会装到别的环境去。"
    }
  </div>

  <section class="card" id="job-box" data-active-node="{attr(active_node)}"{"" if job else " hidden"}>
    <div class="card-head"><h2>执行中的任务</h2></div>
    <div id="job-title">{_job_title(job)}</div>
    <div class="prog" id="job-progress"{"" if running else " hidden"}>
      <div class="track"><div class="fill indeterminate"></div></div>
      <div class="meta"></div>
    </div>
    <pre class="term" id="job-output">{esc(chr(10).join(job.get("output") or [])) if job else ""}</pre>
  </section>

  {_card("HTTP 适配器", _component_cards(http, csrf, bodies, running), hint="curl_cffi 是核心依赖，随主包一起装")}
  {_card("浏览器渲染", _component_cards(browser, csrf, bodies, running), hint="一台机器通常只会用其中一个")}

  {
        ""
        if remote
        else f'''
  <section class="card">
    <div class="card-head"><h2>playwright 与 patchright 的浏览器本体</h2>
      <span class="hint">经常被问：装了一个能不能省掉另一个</span></div>
    <p class="note">
      <b>不能。</b>两者的本体下到<b>同一个目录</b>（<code>{esc(registry_dir)}</code>），
      但各自钉的 chromium <b>版本号不同</b>，所以是两份独立的构建，各约 150–170 MB：
    </p>
    <div class="scroll"><table class="data">
      <thead><tr><th>组件</th><th>它自己那几个 revision</th><th>占用</th></tr></thead>
      <tbody>{revisions}</tbody>
    </table></div>
    <p class="note" style="margin-top:.75rem">
      共用的部分确实只下一份（比如 <code>ffmpeg</code>）。所以两个都装时，第二个的增量
      小于它单独装的体积——但省不掉那份 chromium。
      patchright 是 playwright 的<b>反检测分支</b>，Python 包也是两个独立的发行版，
      同理不能合并。<b>一台机器通常只需要其中一个</b>：要反检测选 patchright，
      要行为最可预期选 playwright。
    </p>
  </section>'''
    }
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


def _component_target(nodes: list[dict[str, Any]], active: str) -> str:
    if not nodes:
        return ""
    options = f'<option value=""{"" if active else " selected"}>本机</option>' + "".join(
        f'<option value="{attr(n.get("id"))}"{" selected" if str(n.get("id")) == active else ""}>'
        f"{esc(n.get('id'))} — {esc(n.get('address'))}{'（本机）' if n.get('is_self') else ''}</option>"
        for n in nodes
    )
    return f"""
  <section class="card">
    <div class="card-head"><h2>装到哪台机器</h2>
      <span class="hint">集群里每台都要各自装一遍——在这里点名，不用逐台 SSH 上去</span></div>
    <form method="get" action="/components" class="filters">
      <div>
        <label for="c-node">目标机器</label>
        <select id="c-node" name="node">{options}</select>
      </div>
      <div class="check"><button type="submit">切换</button></div>
    </form>
    <p class="note" style="margin-top:.5rem">
      子节点默认<b>不允许</b>被远程操作。要放开，在那台机器的配置里设
      <code>[CLUSTER].allow_remote_install = true</code> 并重启——它等于"能调那台
      gRPC 的人可以在它上面跑 pip"，所以不会随升级默认打开。
    </p>
  </section>"""


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
    _ = csrf
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


CONFIG_TABS: tuple[tuple[str, str, str], ...] = (
    ("basic", "基础设置", "这一台自己的端口、线程、超时、日志、浏览器与链路记录"),
    ("cluster", "集群设置", "转发开关、节点增删、以及每台子节点的部署材料"),
)


def _config_tabs(active: str) -> str:
    return (
        '<div class="tabs">'
        + "".join(
            f'<a href="/config?tab={attr(key)}" class="{"on" if key == active else ""}">{esc(label)}</a>'
            for key, label, _ in CONFIG_TABS
        )
        + "</div>"
    )


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
    tab: str = "basic",
    cluster: dict[str, Any] | None = None,
) -> str:
    active = tab if tab in {key for key, _, _ in CONFIG_TABS} else "basic"
    subtitle = next(sub for key, _, sub in CONFIG_TABS if key == active)

    sections = "".join(
        f"<fieldset><legend>{esc(title)}</legend>{''.join(_config_row(field) for field in fields)}</fieldset>"
        for title, fields in groups
    )
    tab_field = f'<input type="hidden" name="tab" value="{attr(active)}">'

    if active == "cluster":
        main = _cluster_tab(cluster or {}, sections, csrf, tab_field)
        extra = ""
    else:
        main = f"""
  <section class="card">
    <div class="card-head">
      <h2>可编辑项</h2>
      <span class="hint">保存后写回 <code>{esc(config_path)}</code>（先留一份 <code>.bak</code>）</span>
    </div>
    <p class="note">文件里的注释与格式都保留，只替换被改动那一行的值。</p>
    <form method="post" action="/config" id="config-form">
      {_hidden(csrf, "save_config")}{tab_field}
      {sections}
    </form>
  </section>"""
        extra = f"""
  {_secret_generators(generators or [], csrf)}

  <section class="card">
    <div class="card-head"><h2>只读项</h2></div>
    <p class="note">这些刻意不可从网页修改：本服务能代任意 URL 发请求，一个能从网页
       关掉内网拦截、改掉令牌的管理端，等于给自己装了个跳板。要改请编辑
       <code>{esc(config_path)}</code> 或 <code>.env</code> 后重启。</p>
    <table class="kv">{_rows(readonly_note)}</table>
  </section>"""

    body = f"""
  {_config_tabs(active)}
  {_messages(messages, errors)}
  {_generated_secret(generated)}
  {main}
  {extra}
"""
    actions = (
        '<span class="note">带 <span class="pill bad restart">需重启</span> 的项改完要重启 ipclick</span>'
        '<button class="primary" type="submit" form="config-form">保存到 toml</button>'
    )
    return _page(
        body,
        username,
        csrf,
        "/config",
        title="配置",
        subtitle=f"{esc(subtitle)} · 写回 <code>{esc(config_path)}</code>",
        actions=actions,
    )


def _cluster_tab(cluster: dict[str, Any], sections: str, csrf: str, tab_field: str) -> str:
    nodes = list(cluster.get("nodes") or [])
    forward = bool(cluster.get("forward"))
    secret_ready = bool(cluster.get("secret_configured"))
    token_ready = bool(cluster.get("auth_configured"))

    cards = "".join(_node_card(node) for node in nodes) or (
        '<p class="note">还没有节点。点右上角「添加节点」，填个 IP 和端口就行——其余用预置默认值。</p>'
    )

    switch = f"""
  <section class="card">
    <div class="card-head"><h2>服务端转发</h2>
      <span class="hint">开了才是"集群模式"——本机收到任务后按策略分给下面的节点</span></div>
    <form method="post" action="/config" id="config-form">
      {_hidden(csrf, "save_config")}{tab_field}
      <div class="field-row">
        <label>模式</label>
        <div class="check-row">
          {
        _checkbox(
            "CLUSTER.forward_on",
            "开启服务端转发",
            forward,
            "关着时本机只处理自己收到的请求，下面的节点仅用于「试一试」点名直连",
        )
    }
        </div>
      </div>
      {sections}
    </form>
  </section>"""

    return f"""
  {switch}

  <section class="card">
    <div class="card-head"><h2>集群节点</h2>
      <span class="hint">{len(nodes)} 台。每台机器上的这份列表都该是一样的</span>
      <div class="head-actions">
        <a class="btn" href="/deploy.zip">全部下载（zip）</a>
        <button type="button" class="primary" data-dialog="add-node">添加节点</button>
      </div>
    </div>
    <div class="nodes-grid">{cards}</div>
    <p class="note" style="margin-top:.75rem">
      改完地址或权重点右上角<b>保存</b>；<b>删除</b>是独立按钮，点了就生效。
      节点的 <code>token</code> 不接受从网页写入——机密只走 <code>.env</code>。
      需要给某台单独指定令牌时，在配置文件里给那一项加 <code>token = "..."</code>。
    </p>
  </section>

  <!-- 删除走自己的表单：和"保存"那个表单分开，否则删一台会连带把页面上其余
       未提交的改动一起写进去，而人只点了「删除」。 -->
  <form method="post" action="/config" id="remove-node-form" hidden>
    {_hidden(csrf, "remove_node")}{tab_field}
  </form>

  {_add_node_dialog(csrf, tab_field, int(cluster.get("next_port") or 0))}

  <section class="card">
    <div class="card-head"><h2>凭据</h2>
      <span class="hint">生成一次，复制到每台子节点的 .env</span></div>
    <table class="kv">
      <tr><th>gRPC 鉴权令牌</th><td>{_pill("已配置", "ok") if token_ready else _pill("未配置 —— 任何人都能调用", "bad")}
        <span class="note">调用方 → 服务端。整个集群用同一个，听主控的。</span></td></tr>
      <tr><th>集群共享密钥</th><td>{_pill("已配置", "ok") if secret_ready else _pill("未配置 —— 节点间不鉴权", "warn")}
        <span class="note">节点 → 节点。由它<b>派生</b>出每台各不相同的令牌，
          所以拿到 B 的令牌调不了 C；而你只需要复制这一个值到所有机器。</span></td></tr>
    </table>
    <p class="note" style="margin-top:.75rem">两个都在下面「基础设置」页的「生成凭据」里一键生成。
      生成的值<b>只显示一次</b>，服务端不留副本。</p>
  </section>"""


def _node_card(node: dict[str, Any]) -> str:
    node_id = str(node.get("id", ""))
    index = node.get("index", 0)
    is_self = bool(node.get("is_self"))
    return f"""
  <div class="node-card" data-node-row>
    <div class="top">
      <span class="nm">{esc(node_id)}</span>
      {_pill("本机", "info") if is_self else ""}
    </div>
    <label>地址</label>
    <input name="node_address_{attr(index)}" value="{attr(node.get("address", ""))}" data-node-address
           form="config-form" placeholder="10.0.0.7:{DEFAULT_GRPC_PORT_HINT}">
    <input type="hidden" name="node_id_{attr(index)}" value="{attr(node_id)}" form="config-form">
    <div class="two-up">
      <div><label>权重</label>
        <input name="node_weight_{attr(index)}" value="{attr(node.get("weight", 100))}" form="config-form"></div>
      <div><label>状态</label><div class="note">{esc(node.get("status") or "未探测")}</div></div>
    </div>
    <div class="acts">
      <button type="button" class="small" data-probe="{attr(node_id)}">测试连接</button>
      <a class="btn small" href="/deploy?node={attr(node_id)}">部署材料</a>
      <button type="submit" class="small danger" form="remove-node-form"
              name="remove_node" value="{attr(node_id)}"
              data-confirm="确定从集群里移除 {attr(node_id)} 吗？（只改本机的节点列表，不动那台机器）"
      >删除</button>
    </div>
    <div class="result" data-probe-result></div>
  </div>"""


def _add_node_dialog(csrf: str, tab_field: str, next_port: int) -> str:
    return f"""
  <div class="dialog" id="add-node" hidden>
    <div class="dialog-box">
      <div class="card-head"><h2>添加节点</h2>
        <button type="button" class="small" data-dialog-close>关闭</button></div>
      <form method="post" action="/config">
        {_hidden(csrf, "add_node")}{tab_field}
        <div class="field-row">
          <label for="n-host">IP / 主机名 <b>*</b></label>
          <input id="n-host" name="new_node_host" required placeholder="10.0.0.7" autocomplete="off">
        </div>
        <div class="field-row">
          <label for="n-port">端口</label>
          <div><input id="n-port" name="new_node_port" value="{next_port}" placeholder="{next_port}">
            <span class="hint">子节点的 gRPC 端口。已自动填了下一个没被占用的</span></div>
        </div>
        <div class="field-row">
          <label for="n-id">节点 id</label>
          <div><input id="n-id" name="new_node_id" placeholder="留空则用 主机:端口" autocomplete="off">
            <span class="hint">链路记录里的"谁执行的"就是它，起个好认的名字</span></div>
        </div>
        <div class="field-row">
          <label for="n-weight">权重</label>
          <div><input id="n-weight" name="new_node_weight" value="100">
            <span class="hint">只在负载均衡策略为 weight 时有意义</span></div>
        </div>
        <div class="actions">
          <button class="primary" type="submit">添加并保存</button>
          <span class="note">会立即写回 toml 并生效，不用重启。</span>
        </div>
      </form>
    </div>
  </div>"""


def _generated_secret(generated: dict[str, Any] | None) -> str:
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
    restart = '<span class="pill bad restart">需重启</span>' if field.get("restart") else ""
    hint = f'<span class="hint">{esc(field["hint"])}</span>' if field.get("hint") else ""

    running = int(field.get("running") or 0)
    if running:
        hint += (
            f'<span class="pill warn running">当前实际在 {running}</span>'
            f'<span class="hint">这一格是<b>文件里</b>的值。命令行的 --port 覆盖了它，'
            f"改这里要连同启动命令一起改，否则重启后又变回 {running}</span>"
        )

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

    return (
        f'<div class="field-row"><label for="{attr(name)}">{esc(field["label"])}{restart}{hint}</label>'
        f"<div>{control}</div></div>"
    )


def render_deploy(
    plan: dict[str, Any],
    username: str,
    csrf: str,
    *,
    total_nodes: int,
) -> str:
    node_id = str(plan.get("node_id", ""))
    commands = "".join(
        f"""
      <label>{esc(item["title"])}</label>
      <div class="copy-row">
        <pre id="cmd-{index}">{esc(item["command"])}</pre>
        <button type="button" class="small" data-copy="cmd-{index}">复制</button>
      </div>"""
        for index, item in enumerate(plan.get("commands") or [])
    )

    body = f"""
  <div class="msg caution">
    <b>下面的 <code>.env</code> 里是真实的令牌。</b>复制到子节点之后请
    <code>chmod 600 .env</code>，并且别把它提交进版本库。
  </div>

  <section class="card">
    <div class="card-head"><h2>{esc(plan.get("toml_name", "ipclick.toml"))}</h2>
      <span class="hint">按端口命名，所以几台节点的配置能并排放在同一个目录里</span>
      <div class="head-actions">
        <button type="button" class="small" data-copy="dep-toml">复制</button>
        <a class="btn small" href="/deploy?node={attr(node_id)}&amp;kind=toml&amp;dl=1"
           download="{attr(plan.get("toml_name", "ipclick.toml"))}">下载</a>
      </div>
    </div>
    <pre id="dep-toml">{esc(plan.get("toml", ""))}</pre>
  </section>

  <section class="card">
    <div class="card-head"><h2>.env</h2>
      <span class="hint">两个值都取自主控当前生效的那份，复制过去必然对得上</span>
      <div class="head-actions">
        <button type="button" class="small" data-copy="dep-env">复制</button>
        <a class="btn small" href="/deploy?node={attr(node_id)}&amp;kind=env&amp;dl=1"
           download=".env">下载</a>
      </div>
    </div>
    <pre id="dep-env">{esc(plan.get("env", ""))}</pre>
  </section>

  <section class="card">
    <div class="card-head"><h2>在那台机器上怎么起</h2>
      <span class="hint">两种写法挑一种：uv 建的 venv 默认不装 pip，反过来很多机器上没有 uv</span></div>
    {commands}
  </section>
"""
    actions = (
        f'<a class="btn" href="/config?tab=cluster">返回集群设置</a>'
        f'<a class="btn primary" href="/deploy.zip">全部 {total_nodes} 台打包下载</a>'
    )
    return _page(
        body,
        username,
        csrf,
        "/config",
        title=f"部署 {node_id}",
        subtitle=f"复制到 <code>{esc(plan.get('address', ''))}</code> 那台机器上",
        actions=actions,
    )


def render_skill(
    markdown: str,
    username: str,
    csrf: str,
    *,
    version: str,
    description: str,
    install_dir: str,
) -> str:
    body = f"""
  <div class="msg tip">
    技能包（Skill）是一份 Markdown：告诉 AI 代理<b>什么时候</b>该用 IPClick、<b>怎么用</b>。
    装进项目之后，直接对它说"用 ipclick 抓一下 …"就行，不必再逐条解释命令。
  </div>

  <section class="card">
    <div class="card-head"><h2>装到项目里</h2>
      <span class="hint">在<b>装了 ipclick 的那个环境</b>里执行</span></div>
    <p class="note">写到 <code>{esc(install_dir)}</code>，Claude Code 这类代理会自动发现它。</p>
    <pre id="skill-install">ipclick skill install</pre>
    <div class="actions">
      <button type="button" data-copy="skill-install">复制命令</button>
      <a class="btn" href="/skill.md" download="SKILL.md">下载 SKILL.md</a>
    </div>
    <p class="note" style="margin-top:.75rem">
      已经存在且被你改过时不会覆盖——要覆盖加 <code>--force</code>。
      升级 IPClick 之后重装一次，用法说明会跟着版本走。
    </p>
  </section>

  <section class="card">
    <div class="card-head"><h2>它会让 AI 知道什么</h2>
      <span class="hint">v{esc(version)}</span></div>
    <p class="note">{esc(description)}</p>
    <p class="note">
      正文里写清了输出契约（<code>--json</code> 时 stdout 只有一个 JSON 文档）、
      五档退出码分别该往哪儿查、以及几个最容易踩的坑
      （响应体默认截断、装包和下浏览器本体是两件事、别猜适配器名）。
    </p>
    <pre id="skill-body">{esc(markdown)}</pre>
    <div class="actions">
      <button type="button" data-copy="skill-body">复制全文</button>
    </div>
  </section>

  <section class="card">
    <div class="card-head"><h2>先让它自检</h2></div>
    <p class="note">技能里第一条就是这句——AI 会先问清楚这台机器能干什么，再决定用哪个适配器：</p>
    <pre id="skill-status">ipclick status --json</pre>
    <div class="actions"><button type="button" data-copy="skill-status">复制</button></div>
  </section>
"""
    return _page(
        body,
        username,
        csrf,
        "/skill",
        title="AI 接入",
        subtitle="把 IPClick 的用法交给 AI 代理——一份随版本走的技能包",
    )


__all__ = [
    "DEFAULT_LIVE_MS",
    "LIVE_INTERVALS",
    "NAV",
    "dashboard_live",
    "esc",
    "live_label",
    "render_components",
    "render_config",
    "render_dashboard",
    "render_login",
    "render_skill",
    "render_test",
    "render_trace",
    "set_default_theme",
    "trace_live",
]
