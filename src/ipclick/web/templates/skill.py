"""内置 AI Skill 预览与安装说明的 HTML 渲染器。"""

from __future__ import annotations

from ipclick.web.templates.base import esc, page


def render_skill(
    markdown: str,
    username: str,
    csrf: str,
    *,
    version: str,
    description: str,
    install_dir: str,
) -> str:
    """渲染 Skill 元数据、安装命令及已转义的 Markdown 原文。"""
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
    return page(
        body,
        username,
        csrf,
        "/skill",
        title="AI 接入",
        subtitle="把 IPClick 的用法交给 AI 代理——一份随版本走的技能包",
    )
