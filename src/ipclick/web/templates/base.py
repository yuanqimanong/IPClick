from __future__ import annotations

from typing import Any

from ipclick.trace import TraceRecord
from ipclick.web.assets import SCRIPT_BOOT, SCRIPT_MAIN, STYLE


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


def icon(name: str) -> str:
    return f'<svg viewBox="0 0 24 24" aria-hidden="true">{_ICONS.get(name, "")}</svg>'


def rows(pairs: list[tuple[str, Any]]) -> str:
    return "".join(f"<tr><th>{esc(k)}</th><td>{v}</td></tr>" for k, v in pairs)


def pill(text: str, kind: str) -> str:
    return f'<span class="pill {kind}">{esc(text)}</span>'


def bool_pill(value: Any, *, good_is_true: bool = True) -> str:
    truthy = bool(value)
    good = truthy if good_is_true else not truthy
    return pill("是" if truthy else "否", "ok" if good else "warn")


def status_pill(status_code: int) -> str:
    if status_code < 0:
        return pill("失败", "bad")
    if status_code < 300:
        return pill(str(status_code), "ok")
    if status_code < 400:
        return pill(str(status_code), "info")
    if status_code < 500:
        return pill(str(status_code), "warn")
    return pill(str(status_code), "bad")


def stat(number: Any, label: str, *, accent: bool = False) -> str:
    cls = "stat accent" if accent else "stat"
    return f'<div class="{cls}"><div class="n">{esc(number)}</div><div class="l">{esc(label)}</div></div>'


def bytes_label(size: Any) -> str:
    try:
        value = float(size)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def hidden_fields(csrf: str, action: str) -> str:
    return (
        f'<input type="hidden" name="csrf_token" value="{attr(csrf)}">'
        f'<input type="hidden" name="action" value="{attr(action)}">'
    )


def messages_block(messages: list[str], errors: list[str]) -> str:
    return "".join(f'<div class="msg good">✓ {esc(m)}</div>' for m in messages) + "".join(
        f'<div class="msg err">{esc(e)}</div>' for e in errors
    )


def card(title: str, body: str, *, hint: str = "", actions: str = "") -> str:
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


def theme_attr(theme: str | None) -> str:
    resolved = theme if theme in ("light", "dark") else _default_theme
    return f' data-default-theme="{attr(resolved)}"'


def page(
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
        f'<a href="{attr(path)}" class="{"on" if path == active else ""}">{icon(name)}<span>{esc(label)}</span></a>'
        for path, label, name in NAV
    )
    shell_class = "shell has-rail" if rail else "shell"
    rail_html = f'<aside class="rail">{rail}</aside>' if rail else ""
    subtitle_html = f'<p class="sub">{subtitle}</p>' if subtitle else ""
    actions_html = f'<div class="head-actions">{actions}</div>' if actions else ""

    return f"""<!doctype html>
<html lang="zh-CN"{theme_attr(theme)}><head>
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


_STATUS_COLORS = {
    "2xx": "var(--ok)",
    "3xx": "var(--accent)",
    "4xx": "var(--warn)",
    "5xx": "var(--bad)",
    "failure": "#8250df",
}


def status_bar(process: dict[str, Any]) -> str:
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


def component_badge(component: dict[str, Any]) -> str:
    if not component.get("package"):
        return pill("未装", "mute")
    if component.get("browser") is False:
        return pill("缺本体", "bad")
    if component.get("browser") is None and component.get("kind") == "browser":
        return pill("本体未知", "warn")
    return pill("可用", "ok")


def trace_table(records: list[TraceRecord], *, show_node: bool = True) -> str:
    if not records:
        return '<p class="note">还没有记录。发一个请求，或用<a href="/test">试一试</a>页面造一个。</p>'
    rows: list[str] = []
    for record in records:
        flags: list[str] = []
        if record.forwarded:
            flags.append(pill("转发", "info"))
        if record.stream:
            flags.append(pill("流式", "mute"))
        if record.attempts > 1:
            flags.append(pill(f"重试 {record.attempts - 1}", "warn"))
        if record.queued_ms > 0:
            flags.append(pill(f"排队 {record.queued_ms}ms", "mute"))
        node_cell = f"<td><code>{esc(record.node_id)}</code></td>" if show_node else ""
        error_row = (
            f'<tr><td colspan="{9 if show_node else 8}" class="err">{esc(record.error)}</td></tr>'
            if record.error
            else ""
        )
        rows.append(
            f'<tr><td class="nowrap"><time datetime="{attr(record.iso)}">{esc(record.when)}</time></td>'
            f"<td>{status_pill(record.status_code)}</td>"
            f"<td>{esc(record.method)}</td>"
            f'<td><span class="url" title="{attr(record.url)}">{esc(record.url or "—")}</span></td>'
            f"<td><code>{esc(record.adapter)}</code></td>"
            f"{node_cell}"
            f'<td class="right nowrap">{record.duration_ms:,} ms</td>'
            f'<td class="right nowrap">{bytes_label(record.size)}</td>'
            f"<td>{' '.join(flags)}</td></tr>{error_row}"
        )
    node_head = "<th>节点</th>" if show_node else ""
    head = (
        f"<tr><th>时间</th><th>状态</th><th>方法</th><th>URL</th><th>适配器</th>{node_head}"
        f'<th class="right">耗时</th><th class="right">大小</th><th></th></tr>'
    )
    return f'<div class="scroll"><table class="data"><thead>{head}</thead><tbody>{"".join(rows)}</tbody></table></div>'


def checkbox(name: str, label: str, checked: bool, hint: str = "") -> str:
    hint_html = f'<span class="hint">{hint}</span>' if hint else ""
    return (
        f'<label class="check-inline"><input type="hidden" name="__present__{attr(name)}" value="1">'
        f'<input type="checkbox" name="{attr(name)}"{" checked" if checked else ""}>'
        f"<span>{esc(label)}</span>{hint_html}</label>"
    )
