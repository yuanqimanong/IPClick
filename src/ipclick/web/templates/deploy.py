"""单节点部署说明页面的 HTML 渲染器。"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ipclick.web.templates.base import attr, esc, page


def render_deploy(
    plan: dict[str, Any],
    username: str,
    csrf: str,
    *,
    total_nodes: int,
) -> str:
    """渲染配置、环境变量和多种启动命令，并对内容统一转义。"""
    node_id = str(plan.get("node_id", ""))
    node_query = quote(node_id, safe="")
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
        <a class="btn small" href="/deploy?node={attr(node_query)}&amp;kind=toml&amp;dl=1"
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
        <a class="btn small" href="/deploy?node={attr(node_query)}&amp;kind=env&amp;dl=1"
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
    return page(
        body,
        username,
        csrf,
        "/config",
        # title 由 page() 自己转义（subtitle 不转义，所以那一行的 esc 是对的）。
        title=f"部署 {node_id}",
        subtitle=f"复制到 <code>{esc(plan.get('address', ''))}</code> 那台机器上",
        actions=actions,
    )
