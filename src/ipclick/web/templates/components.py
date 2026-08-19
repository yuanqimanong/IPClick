from __future__ import annotations

from typing import Any

from ipclick.web.templates.base import (
    attr,
    bytes_label,
    card,
    component_badge,
    esc,
    hidden_fields,
    messages_block,
    page,
    pill,
)


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
            f"<td class='nowrap'>{bytes_label(size) if (size := bodies.get(str(c.get('extra')), ('', 0))[1]) else '—'}</td></tr>"
            for c in shared
        )
        or '<tr><td colspan="3" class="note">两个都没装</td></tr>'
    )

    body = f"""
  {messages_block(messages, errors)}
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

  {card("HTTP 适配器", _component_cards(http, csrf, bodies, running), hint="curl_cffi 是核心依赖，随主包一起装")}
  {card("浏览器渲染", _component_cards(browser, csrf, bodies, running), hint="一台机器通常只会用其中一个")}

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
        f'<form method="post" action="/components" class="inline-form">{hidden_fields(csrf, "refresh")}'
        f'<button type="submit">刷新状态</button></form>'
    )
    return page(
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
        else (pill("成功", "ok") if status == "succeeded" else pill("失败", "bad"))
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
    package_line = f'<div><span class="k">Python 包</span>{pill("已装", "ok") if installed else pill("未装", "mute")}{version}</div>'

    body_line = ""
    if is_browser:
        if not installed:
            badge = pill("—", "mute")
        elif body_state is True:
            badge = pill("已就绪", "ok")
        elif body_state is False:
            badge = pill("未下载", "bad")
        else:
            badge = pill("未知", "warn")
        detail = str(component.get("detail") or "")
        hint = f' <span class="note" title="{attr(detail)}">{esc(detail[:60])}</span>' if detail else ""
        body_line = f'<div><span class="k">浏览器本体</span>{badge}{hint}</div>'

    actions: list[str] = []
    if installed:
        location, size = bodies.get(extra, ("", 0))
        leftover = (
            f"\\n\\n注意：只卸 Python 包。浏览器本体（{bytes_label(size)}，位于 {location}）不会被删除，需要时请自行删除该目录。"
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
        {component_badge(component)}
        <code class="note">ipclick[{esc(extra)}]</code>
      </div>
      <div class="why">{esc(component.get("summary", ""))}</div>
      <div class="levels">{package_line}{body_line}</div>
      <div class="acts">{"".join(actions)}</div>
    </div>"""
