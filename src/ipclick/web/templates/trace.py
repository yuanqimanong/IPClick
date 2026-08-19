from __future__ import annotations

from typing import Any

from ipclick.trace import TraceRecord
from ipclick.web.templates.base import attr, card, esc, page, stat, status_bar, trace_table


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
            stat(f"{int(process.get('total', 0)):,}", "本次启动"),
            stat(process.get("in_flight", 0), "在途请求", accent=True),
            stat(f"{process.get('success_rate', 0)}%", "成功率（本次启动）"),
            *(
                [
                    stat(f"{int(window.get('total', 0)):,}", f"近 {esc(stats.get('window_days', 30))} 天"),
                    stat(f"{window.get('success_rate', 0)}%", "成功率（同期）"),
                ]
                if window
                else []
            ),
        ]
    )

    return f"""
  <div class="stats">{cards}</div>
  <section class="card" style="margin-top:1rem">{status_bar(process)}</section>
  <section class="card">
    <div class="card-head"><h2>请求</h2><span class="hint">{source_note}</span></div>
    {trace_table(records)}
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
    return page(
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
    return card("目标站点排行", table)


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
    return card("按天趋势", table, hint="按服务端本地时区分天")
