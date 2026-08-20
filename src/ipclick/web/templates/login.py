"""Web 管理端登录页面的 HTML 渲染器。"""

from __future__ import annotations

from ipclick.web.assets import SCRIPT_BOOT, SCRIPT_MAIN, STYLE
from ipclick.web.templates.base import esc, theme_attr


def render_login(error: str | None = None, *, theme: str | None = None) -> str:
    """渲染登录表单和已转义的可选错误消息。"""
    error_html = f'<div class="msg err" style="margin-top:1rem">{esc(error)}</div>' if error else ""
    return f"""<!doctype html>
<html lang="zh-CN"{theme_attr(theme)}><head>
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
