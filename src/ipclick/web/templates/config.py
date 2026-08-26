"""基础配置与集群配置标签页的 HTML 渲染器。"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ipclick.ports import DEFAULT_GRPC_PORT
from ipclick.web.templates.base import attr, card, checkbox, esc, hidden_fields, messages_block, page, pill, rows


DEFAULT_GRPC_PORT_HINT = DEFAULT_GRPC_PORT

# 隧道解析框只挂在这一组下面，靠标题匹配——字段分组本来就是按标题组织的。
PROXY_GROUP = "代理"

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
    tunnel: dict[str, Any] | None = None,
) -> str:
    """渲染配置字段、只读安全摘要、凭据生成器与集群节点表格。"""
    active = tab if tab in {key for key, _, _ in CONFIG_TABS} else "basic"
    subtitle = next(sub for key, _, sub in CONFIG_TABS if key == active)

    # 只剩一组时（集群页）默认展开：那时候"折叠"只是多一次点击，没有省下任何篇幅。
    single = len(groups) == 1
    sections = "".join(
        _config_group(
            title,
            fields,
            extra=_tunnel_box(tunnel) if title == PROXY_GROUP and tunnel else "",
            open_default=single or any(field.get("running") for field in fields),
        )
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
      <span class="grow"></span>
      <button class="small" type="button" data-groups="open">全部展开</button>
      <button class="small" type="button" data-groups="close">全部折叠</button>
    </div>
    <p class="note">文件里的注释与格式都保留，只替换被改动那一行的值。
       折叠起来的组照样会提交——它只是收起来了，不是被排除在表单外。</p>
    <form method="post" action="/config" id="config-form">
      {hidden_fields(csrf, "save_config")}{tab_field}
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
    <table class="kv">{rows(readonly_note)}</table>
  </section>"""

    body = f"""
  {_config_tabs(active)}
  {messages_block(messages, errors)}
  {_generated_secret(generated)}
  {main}
  {extra}
"""
    actions = (
        '<span class="note">带 <span class="pill bad restart">需重启</span> 的项改完要重启 ipclick</span>'
        '<button class="primary" type="submit" form="config-form">保存到 toml</button>'
    )
    return page(
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
      {hidden_fields(csrf, "save_config")}{tab_field}
      <div class="field-row">
        <label>模式</label>
        <div class="check-row">
          {
        checkbox(
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
    {hidden_fields(csrf, "remove_node")}{tab_field}
  </form>

  {_add_node_dialog(csrf, tab_field, int(cluster.get("next_port") or 0))}

  <section class="card">
    <div class="card-head"><h2>凭据</h2>
      <span class="hint">生成一次，复制到每台子节点的 .env</span></div>
    <table class="kv">
      <tr><th>gRPC 鉴权令牌</th><td>{pill("已配置", "ok") if token_ready else pill("未配置 —— 任何人都能调用", "bad")}
        <span class="note">调用方 → 服务端。整个集群用同一个，听主控的。</span></td></tr>
      <tr><th>集群共享密钥</th><td>{pill("已配置", "ok") if secret_ready else pill("未配置 —— 节点间不鉴权", "warn")}
        <span class="note">节点 → 节点。由它<b>派生</b>出每台各不相同的令牌，
          所以拿到 B 的令牌调不了 C；而你只需要复制这一个值到所有机器。</span></td></tr>
    </table>
    <p class="note" style="margin-top:.75rem">两个都在下面「基础设置」页的「生成凭据」里一键生成。
      生成的值<b>只显示一次</b>，服务端不留副本。</p>
  </section>"""


def _node_card(node: dict[str, Any]) -> str:
    node_id = str(node.get("id", ""))
    node_query = quote(node_id, safe="")
    index = node.get("index", 0)
    is_self = bool(node.get("is_self"))
    return f"""
  <div class="node-card" data-node-row>
    <div class="top">
      <span class="nm">{esc(node_id)}</span>
      {pill("本机", "info") if is_self else ""}
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
      <a class="btn small" href="/deploy?node={attr(node_query)}">部署材料</a>
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
        {hidden_fields(csrf, "add_node")}{tab_field}
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
        f"{' ' + pill('全集群一致', 'warn') if g.get('shared') else ' ' + pill('本机独有', 'mute')}"
        f'<div class="note">{esc(g.get("note", ""))}</div></td>'
        f"<td><code>{esc(g.get('env'))}</code></td>"
        f"<td>{esc(g.get('source', ''))}</td>"
        f'<td class="right"><form method="post" action="/config" class="inline-form">'
        f"{hidden_fields(csrf, 'generate_secret')}"
        f'<input type="hidden" name="secret" value="{attr(g.get("env"))}">'
        f'<button class="small" type="submit">生成</button></form></td></tr>'
        for g in generators
    )
    head = '<tr><th>凭据</th><th>环境变量</th><th>当前来源</th><th class="right"></th></tr>'
    table = f'<div class="scroll"><table class="data"><thead>{head}</thead><tbody>{rows}</tbody></table></div>'
    return card(
        "生成凭据",
        table + '<p class="note" style="margin-top:.75rem">生成的值<b>只显示一次</b>，'
        "服务端不保存、不写进任何文件——请自己粘进 <code>.env</code>。</p>",
        hint="随机生成一个足够长的值，省得自己想",
    )


def _config_group(title: str, fields: list[dict[str, Any]], *, extra: str = "", open_default: bool = False) -> str:
    """把一组字段渲染成可折叠块。

    用 ``<details>`` 而不是 JS 显隐：闭合状态下里面的 input 仍然在 DOM 里、照样随
    表单提交，所以折叠纯粹是显示层的事，不会出现"收起来的那几项没保存"。
    """
    running = sum(1 for field in fields if field.get("running"))
    restart = sum(1 for field in fields if field.get("restart"))
    badges = ""
    if running:
        badges += pill(f"{running} 项与运行值不一致", "warn")
    elif restart:
        badges += f'<span class="hint">{restart} 项需重启</span>'
    body = extra + "".join(_config_row(field) for field in fields)
    return (
        f'<details class="more group" data-group="{attr(title)}"{" open" if open_default else ""}>'
        f'<summary>{esc(title)}<span class="hint">{len(fields)} 项</span>{badges}</summary>'
        f"{body}</details>"
    )


def _tunnel_box(tunnel: dict[str, Any]) -> str:
    """渲染隧道接入串的粘贴框、格式选择和凭据来源表。"""
    options = "".join(
        f'<option value="{attr(key)}">{esc(label)}</option>' for key, label in tunnel.get("formats") or ()
    )
    source_rows = "".join(
        f"<tr><td>{esc(label)}</td><td><code>{esc(env)}</code></td><td>{esc(source)}</td></tr>"
        for label, env, source in tunnel.get("sources") or ()
    )
    sources = (
        f'<div class="scroll"><table class="data">'
        f"<thead><tr><th>凭据</th><th>环境变量</th><th>当前来源</th></tr></thead>"
        f"<tbody>{source_rows}</tbody></table></div>"
        if source_rows
        else ""
    )
    env_path = str(tunnel.get("env_path") or ".env")
    override = str(tunnel.get("override") or "")
    override_note = (
        f'<div class="msg caution" style="margin-top:.5rem">'
        f"<b>当前 <code>[PROXY].tunnel_server</code> 手写着 <code>{esc(override)}</code>。</b>"
        f"它在拼代理 URL 时压过下面的主机+端口，所以上面这一行显示的是它。"
        f"想改回按主机+端口走，在这里粘一行新地址保存即可——保存会把 tunnel_server 清空。</div>"
        if override
        else ""
    )

    return f"""
      <div class="field-row">
        <label for="proxy-tunnel-format">代理格式
          <span class="hint">服务商给的那一行长什么样。认不出来时会让你手动选</span></label>
        <div><select id="proxy-tunnel-format" name="proxy_tunnel_format">{options}</select></div>
      </div>
      <div class="field-row">
        <label for="proxy-tunnel">隧道代理接入地址
          <span class="hint">整行粘进来，<b>带账号密码</b>也没关系</span></label>
        <div>
          <input id="proxy-tunnel" name="proxy_tunnel" class="mono" autocomplete="off"
                 placeholder="socks5://username:password@gate.example.com:7000"
                 value="{attr(tunnel.get("value") or "")}">
          <p class="note" style="margin:.375rem 0 0">
            保存时自动拆开：<b>协议 / 主机 / 端口</b>写进 toml，<b>账号密码</b>写进
            <code>{esc(env_path)}</code>，下面几格会跟着变成拆出来的值。
            回显时凭据位置只出现 <code>{{IPCLICK_PROXY_AUTH_KEY}}</code> 这样的名字——
            原样交回来就表示"凭据别动"，只想换机器就直接改这一行里的主机端口。
            某一项由真实环境变量提供时不写文件，写了也会被环境变量压过去。
          </p>
          {override_note}
          {sources}
        </div>
      </div>"""


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
