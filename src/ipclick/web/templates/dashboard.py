from __future__ import annotations

from typing import Any

from ipclick.trace import TraceRecord
from ipclick.web.templates.base import (
    attr,
    bool_pill,
    bytes_label,
    card,
    component_badge,
    esc,
    hidden_fields,
    page,
    pill,
    rows,
    stat,
    status_bar,
    trace_table,
)


def _concurrency_shape(server: dict[str, Any]) -> str:
    processes = int(server.get("processes", 1) or 1)
    parts: list[str] = []
    parts.append(f"{processes} 进程" if processes > 1 else "单进程")
    parts.append("异步（实验性）" if server.get("async_mode") else "一请求一线程")
    text = esc(" · ".join(parts))
    if processes > 1:
        text += f'<br><span class="muted">链路记录每进程一份，本页只统计 0 号进程——总量约为实际的 1/{processes}</span>'
    return text


def render_dashboard(snapshot: dict[str, Any], username: str, csrf: str, actions_enabled: bool) -> str:
    if "error" in snapshot:
        return page(
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
        card(
            "各适配器",
            _adapter_table(process.get("by_adapter") or {}),
            hint="本次启动以来的分适配器统计",
        )
    }

  {
        card(
            "最近请求",
            trace_table(recent[:10]),
            actions='<a class="btn small" href="/trace">看完整请求流 →</a>',
        )
    }

  {
        card(
            "集群",
            _cluster_summary(cluster) + _cluster_table(list(cluster.get("nodes") or []), csrf, actions_enabled),
            actions='<a class="btn small" href="/nodes">管理节点 →</a>',
        )
    }
"""

    rail = f"""
  <h2>服务端</h2>
  <table class="kv">{
        rows(
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
        rows(
            [
                ("传输层", esc(security.get("tls", "?"))),
                ("令牌鉴权", bool_pill(security.get("auth"))),
                ("拦截内网地址", bool_pill(security.get("block_private_networks"))),
                ("拦截元数据端点", bool_pill(security.get("block_metadata_endpoints"))),
                ("允许页内 JS", bool_pill(browser.get("allow_scripts"), good_is_true=False)),
                ("集群内部鉴权", bool_pill(cluster.get("internal_auth"))),
            ]
        )
    }</table>
  <p class="note" style="margin-top:.5rem">
    这几项刻意不可从网页修改。<b>机密</b>（令牌、密码、证书内容）一律不在本页显示、
    也不接受从本页写入——请改 <code>.env</code>，需要新值可以在<a href="/config">配置</a>页生成一个。
  </p>

  <h2>链路记录</h2>
  <table class="kv">{
        rows(
            [
                (
                    "数据来源",
                    pill(
                        "SQLite" if recorder.get("source") == "sqlite" else "仅内存",
                        "ok" if recorder.get("source") == "sqlite" else "mute",
                    ),
                ),
                ("内存缓冲", f"{esc(recorder.get('in_memory', 0))} / {esc(recorder.get('memory_size', 0))} 条"),
                ("落盘记录数", f"{int(recorder.get('rows', 0)):,}" if recorder.get("sqlite_enabled") else "—"),
                (
                    "数据文件",
                    (
                        f"<code>{esc(recorder.get('sqlite_path'))}</code>（{bytes_label(recorder.get('db_bytes', 0))}）"
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
        rows(
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

    return page(
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
            stat(f"{total:,}", "本次启动以来请求数"),
            stat(f"{process.get('success_rate', 0)}%", "成功率", accent=True),
            stat(f"{process.get('avg_ms', 0)} ms", "平均耗时"),
            stat(process.get("in_flight", 0), f"在途（峰值 {process.get('peak_in_flight', 0)}）"),
            stat(bytes_label(process.get("bytes", 0)), "累计响应体"),
            stat(_uptime(process.get("uptime_seconds", 0)), "运行时长"),
        ]
    )
    return f'<div class="stats">{cards}</div><div style="margin-top:1rem">{status_bar(process)}</div>'


def _component_summary(components: list[dict[str, Any]]) -> str:
    if not components:
        return '<p class="note">—</p>'
    rows = "".join(
        f"<tr><td><code>{esc(c.get('name'))}</code></td><td class='right'>{component_badge(c)}</td></tr>"
        for c in components
    )
    return f'<table class="kv"><tbody>{rows}</tbody></table>'


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
        return pill("0", "ok")
    return pill(f"{dropped:,}（写盘跟不上）", "bad")


def _adapter_table(by_adapter: dict[str, Any]) -> str:
    if not by_adapter:
        return '<p class="note">暂无数据。</p>'
    rows = "".join(
        f"<tr><td><code>{esc(name)}</code></td>"
        f'<td class="right">{int(data.get("total", 0)):,}</td>'
        f'<td class="right">{int(data.get("ok", 0)):,}</td>'
        f'<td class="right">{int(data.get("failed", 0)):,}</td>'
        f'<td class="right">{esc(data.get("avg_ms", 0))} ms</td>'
        f'<td class="right">{bytes_label(data.get("bytes", 0))}</td></tr>'
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
        pill("服务端转发", "ok") + " 本节点收到任务后按策略分发"
        if forward
        else pill("客户端分发", "mute") + " 本节点只执行自己收到的任务，分发由调用方负责"
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
                f"{hidden_fields(csrf, name)}"
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
            f"<tr><td>{esc(node.get('id'))}{' ' + pill('本机', 'info') if node.get('is_self') else ''}</td>"
            f"<td><code>{esc(node.get('address'))}</code></td>"
            f"<td>{pill(label, kind)}</td>"
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
