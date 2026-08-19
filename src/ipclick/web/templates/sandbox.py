from __future__ import annotations

from typing import Any

from ipclick.web.templates.base import (
    attr,
    bool_pill,
    bytes_label,
    checkbox,
    esc,
    hidden_fields,
    page,
    rows,
    status_pill,
)


TEST_RETRIES_MAX_HINT = 5

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
      {hidden_fields(csrf, "import_curl")}
      <textarea name="curl" rows="3"
        placeholder="curl 'https://example.com/api' -X POST -H 'content-type: application/json' --data-raw '{{}}'"
      ></textarea>
      <div class="actions"><button type="submit">解析并填入下面的表单</button></div>
    </form>
  </section>

  <section class="card">
    <div class="card-head"><h2>请求</h2></div>
    <form method="post" action="/test">
      {hidden_fields(csrf, "test")}
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
    return page(body, username, csrf, "/test", title="试一试", subtitle="就地发一次请求，看链路与返回的源码")


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
          {checkbox("verify", "校验目标站点证书", form.get("verify", "on") == "on")}
          {checkbox("allow_redirects", "跟随重定向", form.get("allow_redirects", "on") == "on")}
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
    trace_rows = rows(
        [
            ("状态码", status_pill(int(result.get("status_code", -1)))),
            ("实际 URL", f"<code>{esc(result.get('effective_url', ''))}</code>"),
            ("耗时", f"{int(result.get('elapsed_ms', 0)):,} ms"),
            ("响应体大小", bytes_label(result.get("size", 0))),
            ("执行节点", f"<code>{esc(trace.get('node_id') or '—')}</code>"),
            ("实际适配器", f"<code>{esc(trace.get('adapter') or '—')}</code>"),
            ("尝试次数", esc(trace.get("attempts", 1))),
            ("经由转发", bool_pill(trace.get("forwarded"), good_is_true=False)),
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
        f"（共 {bytes_label(result.get('size', 0))}）。</p>"
        if result.get("truncated")
        else ""
    )
    return f"""
  <section class="card">
    <div class="card-head"><h2>结果</h2></div>
    {error}
    <div class="grid two">
      <div><h3 class="sub-head">链路</h3>
        <table class="kv">{trace_rows}</table></div>
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
