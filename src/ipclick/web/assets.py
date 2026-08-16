"""Web 端的 CSS 与 JavaScript。

从 :mod:`ipclick.web.templates` 拆出来，理由很简单：那边是"页面长什么结构"，
这边是"页面长什么样、怎么动"。混在一个文件里的话，改一行按钮样式要在两千行
HTML 拼接中间找那段 CSS。

零前端依赖仍然成立
------------------
没有模板引擎、没有框架、没有打包工具，也没有任何外部资源。布局是纯 CSS Grid，
交互是几十行原生 JS。0.3 时页面里**一行 JS 都没有**（自动刷新靠
``<meta refresh>``），0.4 加了——因为有三件事没有 JS 就做不好：

* **手动切主题。** ``prefers-color-scheme`` 只能跟随系统，而办公室的显示器和
  夜里的笔记本需要的往往不是同一个。
* **装依赖要轮询。** ``camoufox fetch`` 要下 1 GB，只能后台跑 + 查状态。
* **请求流实时刷新。** ``<meta refresh>`` 每 3 秒重载整页：滚动位置丢失、
  正在填的过滤条件被冲掉、页面白闪。换成局部替换之后这三条全没了。

CSP 用哈希而不是 'unsafe-inline'
--------------------------------
脚本是写死在源码里的常量，所以可以算出它的 sha256 放进
``script-src``。这样即使某处转义漏了、注入进一行 ``<script>``，它也执行不了——
而 ``'unsafe-inline'`` 会把这层保护整个让开。
"""

from __future__ import annotations

import base64
import hashlib


# --------------------------------------------------------------------------- #
# 样式
# --------------------------------------------------------------------------- #

STYLE = """
/* ---------- 设计变量 ----------
   亮色是基准，暗色只覆盖变量。三态主题：
     data-theme 未设置 -> 跟随系统（prefers-color-scheme）
     data-theme="light" -> 强制亮
     data-theme="dark"  -> 强制暗
   注意暗色的媒体查询要写成 :root:not([data-theme="light"])，
   否则用户在暗色系统里手动选"亮"会选不动。                                */
:root {
  color-scheme: light dark;
  --bg: #ffffff;
  --bg-soft: #f6f8fa;
  --bg-elev: #ffffff;
  --bg-sunk: #eef1f4;
  --fg: #1f2328;
  --fg-dim: #59636e;
  --fg-faint: #848d97;
  --line: #d1d9e0;
  --line-soft: #e6eaef;
  --accent: #0969da;
  --accent-fg: #ffffff;
  --accent-soft: #ddf4ff;
  --ok: #1a7f37;      --ok-bg: #dafbe1;
  --bad: #cf222e;     --bad-bg: #ffebe9;
  --warn: #9a6700;    --warn-bg: #fff8c5;
  --info: #0550ae;    --info-bg: #ddf4ff;
  --shadow: 0 1px 2px rgba(31,35,40,.06), 0 3px 8px rgba(31,35,40,.05);
  --shadow-lg: 0 8px 28px rgba(31,35,40,.14);
  --radius: 10px;
  --radius-sm: 6px;
  --rail: 20rem;
  --side: 13.5rem;
}

@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0d1117;
    --bg-soft: #161b22;
    --bg-elev: #151b23;
    --bg-sunk: #0b0f14;
    --fg: #e6edf3;
    --fg-dim: #9198a1;
    --fg-faint: #6e7681;
    --line: #3d444d;
    --line-soft: #262c34;
    --accent: #4493f8;
    --accent-fg: #ffffff;
    --accent-soft: #0c2d6b;
    --ok: #3fb950;    --ok-bg: #0f2f18;
    --bad: #ff7b72;   --bad-bg: #3c1618;
    --warn: #d29922;  --warn-bg: #3a2d10;
    --info: #79c0ff;  --info-bg: #0c2d6b;
    --shadow: 0 1px 2px rgba(1,4,9,.5), 0 3px 8px rgba(1,4,9,.4);
    --shadow-lg: 0 8px 28px rgba(1,4,9,.7);
  }
}

:root[data-theme="dark"] {
  --bg: #0d1117;
  --bg-soft: #161b22;
  --bg-elev: #151b23;
  --bg-sunk: #0b0f14;
  --fg: #e6edf3;
  --fg-dim: #9198a1;
  --fg-faint: #6e7681;
  --line: #3d444d;
  --line-soft: #262c34;
  --accent: #4493f8;
  --accent-fg: #ffffff;
  --accent-soft: #0c2d6b;
  --ok: #3fb950;    --ok-bg: #0f2f18;
  --bad: #ff7b72;   --bad-bg: #3c1618;
  --warn: #d29922;  --warn-bg: #3a2d10;
  --info: #79c0ff;  --info-bg: #0c2d6b;
  --shadow: 0 1px 2px rgba(1,4,9,.5), 0 3px 8px rgba(1,4,9,.4);
  --shadow-lg: 0 8px 28px rgba(1,4,9,.7);
  color-scheme: dark;
}

:root[data-theme="light"] { color-scheme: light; }

/* ---------- 基础 ---------- */
*, *::before, *::after { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--fg);
  font: 14px/1.6 ui-sans-serif, system-ui, -apple-system, "Segoe UI", "PingFang SC",
        "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  font-variant-numeric: tabular-nums;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
code, pre, .mono { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace; }
code { font-size: .85em; }
h1, h2, h3, h4 { margin: 0; font-weight: 600; line-height: 1.3; }
p { margin: 0 0 .75rem; }
p:last-child { margin-bottom: 0; }
hr { border: 0; border-top: 1px solid var(--line-soft); margin: 1.25rem 0; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }

/* ---------- 骨架 ----------
   0.3 是单栏纵向堆叠：总览页把服务器信息、各适配器、渲染引擎、集群、最近请求
   全挤在一条竖线上，只能一路往下滚。改成左导航 + 主内容 + 右状态栏三栏。     */
.shell {
  display: grid;
  grid-template-columns: var(--side) minmax(0, 1fr);
  grid-template-areas: "side main";
  min-height: 100vh;
}
.shell.has-rail {
  grid-template-columns: var(--side) minmax(0, 1fr) var(--rail);
  grid-template-areas: "side main rail";
}

/* ---------- 侧栏 ---------- */
.side {
  grid-area: side;
  background: var(--bg-soft);
  border-right: 1px solid var(--line-soft);
  padding: 1rem .75rem;
  display: flex;
  flex-direction: column;
  gap: .25rem;
  position: sticky;
  top: 0;
  height: 100vh;
  overflow-y: auto;
}
.brand { display: flex; align-items: center; gap: .5rem; padding: .25rem .5rem 1rem; }
.brand .mark {
  width: 1.75rem; height: 1.75rem; border-radius: var(--radius-sm);
  background: var(--accent); color: var(--accent-fg);
  display: grid; place-items: center; font-weight: 700; font-size: .8125rem; flex: 0 0 auto;
}
.brand .name { font-weight: 600; font-size: .9375rem; }
.brand .ver { color: var(--fg-faint); font-size: .6875rem; }
.side nav { display: flex; flex-direction: column; gap: .125rem; }
.side nav a {
  display: flex; align-items: center; gap: .625rem;
  padding: .5rem .625rem; border-radius: var(--radius-sm);
  color: var(--fg-dim); font-size: .875rem; text-decoration: none;
}
.side nav a:hover { background: var(--bg-sunk); color: var(--fg); text-decoration: none; }
.side nav a.on { background: var(--accent); color: var(--accent-fg); font-weight: 600; }
.side nav a svg { width: 1rem; height: 1rem; flex: 0 0 auto; stroke: currentColor; fill: none;
                  stroke-width: 1.75; stroke-linecap: round; stroke-linejoin: round; }
.side .spacer { flex: 1 1 auto; min-height: 1rem; }
.side .foot { border-top: 1px solid var(--line-soft); padding-top: .75rem; display: grid; gap: .5rem; }
.who { padding: 0 .5rem; font-size: .75rem; color: var(--fg-faint); word-break: break-all; }

/* ---------- 主区 ---------- */
.main { grid-area: main; padding: 1.5rem 1.75rem 4rem; min-width: 0; }
.rail {
  grid-area: rail; padding: 1.5rem 1.25rem 4rem; min-width: 0;
  border-left: 1px solid var(--line-soft); background: var(--bg-soft);
}
/* 右栏只有 20rem 宽，键列再占 11rem 就没地方放值了 */
.rail table.kv th { width: 6.5rem; font-size: .75rem; }
.rail table.kv td { font-size: .8125rem; }
.rail h2 { font-size: .8125rem; color: var(--fg-dim); margin-bottom: .625rem; }
.rail h2 + table, .rail h2 + p { margin-top: 0; }
.rail section + h2, .rail table + h2, .rail p + h2 { margin-top: 1.5rem; }
.page-head { display: flex; align-items: flex-start; justify-content: space-between;
             gap: 1rem; flex-wrap: wrap; margin-bottom: 1.25rem; }
.page-head h1 { font-size: 1.125rem; }
.page-head .sub { color: var(--fg-dim); font-size: .8125rem; margin: .25rem 0 0; }
.head-actions { display: flex; gap: .5rem; align-items: center; flex-wrap: wrap; }

/* ---------- 卡片 ---------- */
.card {
  background: var(--bg-elev); border: 1px solid var(--line-soft);
  border-radius: var(--radius); padding: 1.125rem 1.25rem; box-shadow: var(--shadow);
}
.card + .card { margin-top: 1rem; }
.card > h2, .card > .card-head h2 { font-size: .9375rem; }
.card-head { display: flex; align-items: center; justify-content: space-between;
             gap: .75rem; flex-wrap: wrap; margin-bottom: .875rem; }
.card-head .hint { color: var(--fg-dim); font-size: .75rem; }
.sub-head { font-size: .8125rem; color: var(--fg-dim); margin-bottom: .5rem; }
.grid { display: grid; gap: 1rem; }
.grid.two { grid-template-columns: repeat(auto-fit, minmax(19rem, 1fr)); }
.grid.three { grid-template-columns: repeat(auto-fit, minmax(13rem, 1fr)); }

/* ---------- 指标 ---------- */
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(8.5rem, 1fr)); gap: .75rem; }
.stat {
  background: var(--bg-elev); border: 1px solid var(--line-soft);
  border-radius: var(--radius); padding: .75rem .875rem; box-shadow: var(--shadow);
}
.stat .n { font-size: 1.5rem; font-weight: 650; line-height: 1.15; letter-spacing: -.02em; }
.stat .l { color: var(--fg-dim); font-size: .6875rem; margin-top: .25rem; }
.stat.accent .n { color: var(--accent); }

/* ---------- 徽标 ---------- */
.pill {
  display: inline-flex; align-items: center; gap: .25rem;
  padding: .0625rem .5rem; border-radius: 2rem;
  font-size: .6875rem; font-weight: 600; white-space: nowrap; line-height: 1.6;
}
.ok   { color: var(--ok);   background: var(--ok-bg); }
.bad  { color: var(--bad);  background: var(--bad-bg); }
.warn { color: var(--warn); background: var(--warn-bg); }
.info { color: var(--info); background: var(--info-bg); }
.mute { color: var(--fg-dim); background: var(--bg-sunk); }
.dot { width: .4375rem; height: .4375rem; border-radius: 50%; background: currentColor; flex: 0 0 auto; }

/* ---------- 表格 ---------- */
.scroll { overflow-x: auto; margin: 0 -.25rem; padding: 0 .25rem; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: .4375rem .625rem; border-bottom: 1px solid var(--line-soft);
         vertical-align: top; }
thead th { font-weight: 600; color: var(--fg-dim); font-size: .75rem; white-space: nowrap;
           border-bottom-color: var(--line); }
tbody tr:last-child > td { border-bottom: none; }
tbody tr:hover > td { background: var(--bg-soft); }
table.kv { table-layout: fixed; }
table.kv th { width: 11rem; font-weight: 500; color: var(--fg-dim); }
table.data td { font-size: .8125rem; }
/* 值里常有长到没有断点的东西：sqlite 的绝对路径、gRPC 的错误串、node id。
   不允许它们在单元格里换行的话，表格会把整个页面撑出横向滚动条——而横向滚
   动条一出现，右侧那栏就永远差一截看不见。 */
td { overflow-wrap: anywhere; }
td code { overflow-wrap: anywhere; }
.right { text-align: right; }
.nowrap { white-space: nowrap; }
.url { display: inline-block; max-width: 26rem; overflow: hidden;
       text-overflow: ellipsis; white-space: nowrap; vertical-align: bottom; }

/* ---------- 表单 ---------- */
label { display: block; font-size: .8125rem; color: var(--fg-dim); margin: 0 0 .25rem; }
input, select, textarea {
  width: 100%; padding: .4375rem .625rem; border: 1px solid var(--line);
  border-radius: var(--radius-sm); font: inherit; background: var(--bg);
  color: inherit; transition: border-color .12s, box-shadow .12s;
}
input:focus, select:focus, textarea:focus {
  outline: none; border-color: var(--accent);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 22%, transparent);
}
input[type=checkbox] { width: auto; accent-color: var(--accent); }
textarea { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .8125rem;
           resize: vertical; }
select:disabled, option:disabled { color: var(--fg-faint); }
button, .btn {
  display: inline-flex; align-items: center; justify-content: center; gap: .375rem;
  padding: .4375rem .875rem; border: 1px solid var(--line); border-radius: var(--radius-sm);
  font: inherit; font-size: .8125rem; cursor: pointer; background: var(--bg-elev);
  color: inherit; text-decoration: none; white-space: nowrap;
  transition: background .12s, border-color .12s;
}
button:hover, .btn:hover { background: var(--bg-soft); border-color: var(--fg-faint); text-decoration: none; }
button.primary { background: var(--accent); border-color: var(--accent); color: var(--accent-fg); font-weight: 600; }
button.primary:hover { filter: brightness(1.08); background: var(--accent); }
button.danger { color: var(--bad); border-color: color-mix(in srgb, var(--bad) 40%, var(--line)); }
button.small, .btn.small { padding: .1875rem .5rem; font-size: .75rem; }
button:disabled { opacity: .5; cursor: not-allowed; }
.field-row {
  display: grid; grid-template-columns: minmax(9rem, 15rem) minmax(0, 1fr);
  gap: .75rem 1rem; align-items: start; padding: .5rem 0;
  border-bottom: 1px solid var(--line-soft);
}
.field-row:last-child { border-bottom: none; }
.field-row > label { margin: .375rem 0 0; color: inherit; font-size: .875rem; }
.field-row .hint { display: block; color: var(--fg-dim); font-size: .75rem; margin-top: .125rem;
                   font-weight: 400; }
.actions { display: flex; gap: .625rem; align-items: center; flex-wrap: wrap; margin-top: 1rem; }
.inline-form { display: inline; }
fieldset { border: 1px solid var(--line-soft); border-radius: var(--radius);
           padding: .25rem 1.125rem 1rem; margin: 0 0 1rem; }
legend { font-size: .8125rem; font-weight: 600; padding: 0 .375rem; color: var(--fg-dim); }
.filters { display: flex; gap: .625rem; align-items: flex-end; flex-wrap: wrap; }
.filters > div { flex: 0 0 auto; }
.filters input, .filters select { width: auto; min-width: 8rem; }
.filters .check { display: flex; align-items: center; gap: .375rem; padding-bottom: .4375rem; }
.filters .check label { margin: 0; }

/* ---------- 提示条 ---------- */
.note { color: var(--fg-dim); font-size: .8125rem; }
.msg { border-radius: var(--radius-sm); padding: .625rem .875rem; font-size: .8125rem;
       margin-bottom: .75rem; border: 1px solid transparent; }
.msg.good { color: var(--ok);   background: var(--ok-bg);
            border-color: color-mix(in srgb, var(--ok) 25%, transparent); }
.msg.err  { color: var(--bad);  background: var(--bad-bg);
            border-color: color-mix(in srgb, var(--bad) 25%, transparent); }
.msg.tip  { color: var(--info); background: var(--info-bg);
            border-color: color-mix(in srgb, var(--info) 25%, transparent); }
.msg.caution { color: var(--warn); background: var(--warn-bg);
               border-color: color-mix(in srgb, var(--warn) 30%, transparent); }
.err { color: var(--bad); font-size: .8125rem; }

/* ---------- 分布条 ---------- */
.bar { display: flex; height: .5rem; border-radius: 2rem; overflow: hidden;
       background: var(--bg-sunk); margin: .5rem 0 .375rem; }
.bar i { display: block; }
.legend { display: flex; gap: .875rem; flex-wrap: wrap; font-size: .75rem; color: var(--fg-dim); }
.legend b { font-weight: 600; color: var(--fg); }

pre {
  background: var(--bg-sunk); border: 1px solid var(--line-soft); border-radius: var(--radius-sm);
  padding: .75rem .875rem; overflow: auto; max-height: 32rem; font-size: .75rem; line-height: 1.55;
  /* 压缩过的 HTML 常常整页就一行，不折行的话只能横向拖着看 */
  white-space: pre-wrap; word-break: break-word; margin: 0;
}
pre.term { background: #0b0f14; color: #d7dee7; border-color: #21262d; max-height: 20rem;
           white-space: pre; word-break: normal; }

/* ---------- 组件卡 ---------- */
.components { display: grid; gap: .75rem; grid-template-columns: repeat(auto-fit, minmax(20rem, 1fr)); }
.comp {
  border: 1px solid var(--line-soft); border-radius: var(--radius); padding: .875rem 1rem;
  background: var(--bg-elev); display: flex; flex-direction: column; gap: .5rem;
}
.comp.ready { border-color: color-mix(in srgb, var(--ok) 35%, var(--line-soft)); }
.comp .top { display: flex; align-items: center; gap: .5rem; flex-wrap: wrap; }
.comp .top .nm { font-weight: 600; font-size: .875rem; }
.comp .why { color: var(--fg-dim); font-size: .75rem; }
.comp .levels { display: grid; gap: .25rem; font-size: .75rem; }
.comp .levels > div { display: flex; align-items: center; gap: .5rem; }
.comp .levels .k { color: var(--fg-dim); min-width: 5.5rem; }
.comp .acts { display: flex; gap: .375rem; flex-wrap: wrap; margin-top: auto; padding-top: .25rem; }

/* ---------- 主题切换 ---------- */
.theme { display: flex; gap: .125rem; background: var(--bg-sunk); border-radius: 2rem; padding: .1875rem; }
.theme button {
  border: none; background: transparent; padding: .25rem .5rem; border-radius: 2rem;
  color: var(--fg-dim); font-size: .6875rem; line-height: 1.4;
}
.theme button:hover { background: transparent; color: var(--fg); }
.theme button[aria-pressed="true"] { background: var(--bg-elev); color: var(--fg);
                                     box-shadow: var(--shadow); font-weight: 600; }

/* ---------- 登录 ---------- */
.login-wrap { min-height: 100vh; display: grid; place-items: center; padding: 1.5rem;
              background: var(--bg-soft); }
.login { width: 100%; max-width: 21rem; background: var(--bg-elev); border: 1px solid var(--line-soft);
         border-radius: var(--radius); padding: 1.75rem; box-shadow: var(--shadow-lg); }
.login .brand { justify-content: center; padding-bottom: 1.25rem; }
.login label { margin-top: .875rem; }

/* ---------- 就地结果（测试连接 / 安装任务） ---------- */
.result { font-size: .75rem; margin-top: .375rem; }
.result:empty { display: none; }
.spin { display: inline-block; width: .75rem; height: .75rem; border: 2px solid var(--line);
        border-top-color: var(--accent); border-radius: 50%; animation: spin .7s linear infinite;
        vertical-align: -2px; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation-duration: 3s; } }

/* ---------- 响应式 ----------
   宽度不够时右栏先降级成主区末尾的一块，再把侧栏压成顶部横条。
   两级断点是因为这两栏的作用完全不同：右栏是"顺带看一眼"，可以下移；
   导航必须一直够得着。                                                     */
@media (max-width: 1200px) {
  .shell.has-rail { grid-template-columns: var(--side) minmax(0, 1fr);
                    grid-template-areas: "side main" "side rail"; }
  .rail { border-left: none; border-top: 1px solid var(--line-soft); padding-top: 1.25rem; }
}
@media (max-width: 820px) {
  .shell, .shell.has-rail {
    grid-template-columns: minmax(0, 1fr);
    grid-template-areas: "side" "main" "rail";
  }
  .side { position: static; height: auto; border-right: none;
          border-bottom: 1px solid var(--line-soft); padding: .75rem; }
  .side nav { flex-direction: row; overflow-x: auto; gap: .25rem; }
  .side nav a { white-space: nowrap; }
  .side .spacer { display: none; }
  .side .foot { border-top: none; padding-top: .5rem; }
  .brand { padding-bottom: .625rem; }
  .main { padding: 1.125rem 1rem 3rem; }
  .rail { padding: 0 1rem 3rem; }
  .field-row { grid-template-columns: minmax(0, 1fr); gap: .25rem; }
  .url { max-width: 14rem; }
}
"""


# --------------------------------------------------------------------------- #
# 脚本
# --------------------------------------------------------------------------- #

#: 主题引导。必须在 <head> 里、渲染之前执行，否则暗色偏好的用户会先看到一闪
#: 而过的白屏（FOUC）。只做一件事，所以单独一段、单独一个哈希。
SCRIPT_BOOT = """
(function () {
  try {
    var saved = localStorage.getItem('ipclick-theme');
    if (saved === 'dark' || saved === 'light') {
      document.documentElement.setAttribute('data-theme', saved);
    }
  } catch (e) { /* 隐私模式下 localStorage 会抛，跟随系统即可 */ }
})();
"""

#: 页面交互。刻意没有事件属性（onclick=...）：那需要 CSP 里的
#: 'unsafe-hashes'，等于把内联脚本的口子重新开一条。全部用 addEventListener +
#: data-* 属性绑定。
SCRIPT_MAIN = """
(function () {
  'use strict';

  // ---------- 主题：跟随系统 / 亮 / 暗 ----------
  var KEY = 'ipclick-theme';
  function apply(mode) {
    if (mode === 'auto') {
      document.documentElement.removeAttribute('data-theme');
    } else {
      document.documentElement.setAttribute('data-theme', mode);
    }
    var buttons = document.querySelectorAll('[data-theme-set]');
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].setAttribute('aria-pressed', String(buttons[i].dataset.themeSet === mode));
    }
  }
  function current() {
    try { return localStorage.getItem(KEY) || 'auto'; } catch (e) { return 'auto'; }
  }
  document.addEventListener('click', function (event) {
    var target = event.target.closest ? event.target.closest('[data-theme-set]') : null;
    if (!target) return;
    var mode = target.dataset.themeSet;
    try { mode === 'auto' ? localStorage.removeItem(KEY) : localStorage.setItem(KEY, mode); } catch (e) {}
    apply(mode);
  });
  apply(current());

  // ---------- 复制 ----------
  document.addEventListener('click', function (event) {
    var button = event.target.closest ? event.target.closest('[data-copy]') : null;
    if (!button) return;
    var source = document.getElementById(button.dataset.copy);
    if (!source) return;
    var text = source.value !== undefined ? source.value : source.textContent;
    var done = function () {
      var original = button.textContent;
      button.textContent = '已复制';
      setTimeout(function () { button.textContent = original; }, 1500);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { fallback(source, done); });
    } else {
      fallback(source, done);
    }
  });
  function fallback(source, done) {
    // http 页面（非 localhost）拿不到 navigator.clipboard，退回选中让用户自己按 Ctrl+C
    if (source.select) { source.select(); }
    try { document.execCommand('copy'); done(); } catch (e) { /* 已经选中了，够用 */ }
  }

  // ---------- 就地 POST（测试连接 / 装卸依赖） ----------
  // 一律带上 CSRF，和普通表单走同一道校验。
  function post(url, payload) {
    var body = new URLSearchParams(payload);
    var token = document.body.dataset.csrf;
    if (token) body.set('csrf_token', token);
    return fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: body.toString()
    }).then(function (response) {
      if (!response.ok) throw new Error('HTTP ' + response.status);
      return response.json();
    });
  }

  document.addEventListener('click', function (event) {
    var button = event.target.closest ? event.target.closest('[data-probe]') : null;
    if (!button) return;
    event.preventDefault();
    var row = button.closest('[data-node-row]');
    var slot = row ? row.querySelector('[data-probe-result]') : null;
    var nodeId = button.dataset.probe;
    var address = row ? (row.querySelector('[data-node-address]') || {}).value : '';
    button.disabled = true;
    if (slot) slot.innerHTML = '<span class="spin"></span> 探测中…';
    post('/api/nodes/probe', { node_id: nodeId, address: address || '' })
      .then(function (data) {
        if (!slot) return;
        var kind = data.ok ? (data.warn ? 'warn' : 'ok') : 'bad';
        slot.innerHTML = '<span class="pill ' + kind + '">' + esc(data.title) + '</span> ' +
                         '<span class="note">' + esc(data.detail) + '</span>';
      })
      .catch(function (error) {
        if (slot) slot.innerHTML = '<span class="pill bad">探测失败</span> <span class="note">' +
                                   esc(String(error)) + '</span>';
      })
      .then(function () { button.disabled = false; });
  });

  document.addEventListener('click', function (event) {
    var button = event.target.closest ? event.target.closest('[data-install]') : null;
    if (!button) return;
    event.preventDefault();
    var confirmText = button.dataset.confirm;
    if (confirmText && !window.confirm(confirmText)) return;
    var buttons = document.querySelectorAll('[data-install]');
    for (var i = 0; i < buttons.length; i++) buttons[i].disabled = true;
    post('/api/components/action', { op: button.dataset.install, extra: button.dataset.extra })
      .then(function (data) {
        showJob(data.job, data.message, data.ok);
        if (data.ok) pollJob();
        else for (var i = 0; i < buttons.length; i++) buttons[i].disabled = false;
      })
      .catch(function (error) {
        showJob(null, String(error), false);
        for (var i = 0; i < buttons.length; i++) buttons[i].disabled = false;
      });
  });

  function showJob(job, message, ok) {
    var box = document.getElementById('job-box');
    if (!box) return;
    box.hidden = false;
    var title = document.getElementById('job-title');
    var output = document.getElementById('job-output');
    if (job) {
      var badge = job.status === 'running'
        ? '<span class="spin"></span> 执行中'
        : (job.status === 'succeeded' ? '<span class="pill ok">成功</span>' : '<span class="pill bad">失败</span>');
      title.innerHTML = badge + ' <b>' + esc(job.title) + '</b> <span class="note">' +
                        esc(job.elapsed + 's · ' + job.command) + '</span>';
      output.textContent = (job.output || []).join('\\n');
      output.scrollTop = output.scrollHeight;
    } else {
      title.innerHTML = '<span class="pill ' + (ok ? 'info' : 'bad') + '">' + esc(message || '') + '</span>';
    }
  }

  var pollTimer = null;
  function pollJob() {
    if (pollTimer) return;
    pollTimer = setInterval(function () {
      fetch('/api/components/status', { credentials: 'same-origin' })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!data.job) return;
          showJob(data.job, '', true);
          if (data.job.status !== 'running') {
            clearInterval(pollTimer);
            pollTimer = null;
            // 装完/卸完重新加载：安装状态、适配器下拉、注册表全都变了，
            // 局部改几个徽标不如让服务端重新渲染一次准。
            setTimeout(function () { window.location.reload(); }, 900);
          }
        })
        .catch(function () { clearInterval(pollTimer); pollTimer = null; });
    }, 1200);
  }
  // 页面加载时如果有任务正在跑（比如刚点完就刷新了），接着轮询
  if (document.getElementById('job-box') && document.body.dataset.jobRunning === '1') {
    showJob(null, '正在执行…', true);
    pollJob();
  }

  // ---------- 实时刷新 ----------
  // 0.3 用的是 <meta refresh> 整页重载：滚动位置丢失、正在填的过滤条件被冲掉、
  // 每 3 秒白闪一次。改成只换那一块的 HTML，服务端仍然负责渲染（不在 JS 里
  // 复制一份渲染逻辑）。
  var live = document.querySelector('[data-live-src]');
  if (live) {
    var interval = Math.max(1000, parseInt(live.dataset.liveInterval || '3000', 10));
    setInterval(function () {
      if (document.hidden) return;  // 后台标签页不刷，别白占服务端 worker
      fetch(live.dataset.liveSrc, { credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.text() : Promise.reject(r.status); })
        .then(function (html) { live.innerHTML = html; })
        .catch(function () { /* 一次失败无所谓，下一轮再来 */ });
    }, interval);
  }

  function esc(text) {
    return String(text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
})();
"""


def sha256_source(script: str) -> str:
    """算 CSP 的 ``'sha256-...'`` 源表达式。

    浏览器算的是 ``<script>`` 标签**内部的字节**，所以这里的输入必须和最终写进
    HTML 的字符串**逐字节一致**——前后多一个换行都会让哈希对不上，页面上表现为
    脚本被静默拦掉（主题切换没反应、安装任务不轮询）。因此模板里插入脚本时不做
    任何缩进或美化。
    """
    digest = hashlib.sha256(script.encode("utf-8")).digest()
    return f"'sha256-{base64.b64encode(digest).decode('ascii')}'"


def csp() -> str:
    """页面的 Content-Security-Policy。

    ``script-src`` 用两段内联脚本的哈希，而不是 ``'unsafe-inline'``：脚本是源码
    里的常量，哈希能精确放行它们，同时让任何注入进来的 ``<script>`` 执行不了。

    ``style-src`` 仍然是 ``'unsafe-inline'``：页面里有大量 ``style="width:37%"``
    这类**属性**（分布条、趋势图的宽度），而哈希覆盖不了行内样式属性，那需要
    ``'unsafe-hashes'``——那个口子比 ``'unsafe-inline'`` 还含糊。样式注入的危害
    也远小于脚本注入。

    ``connect-src 'self'`` 是新加的：0.4 有了 fetch（轮询安装状态、实时刷新），
    要把它限死在本源，免得万一被注入了什么东西能往外发数据。
    """
    return (
        "default-src 'none'; "
        f"script-src {sha256_source(SCRIPT_BOOT)} {sha256_source(SCRIPT_MAIN)}; "
        "style-src 'unsafe-inline'; "
        "connect-src 'self'; "
        "img-src data:; "
        "form-action 'self'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'"
    )


__all__ = ["SCRIPT_BOOT", "SCRIPT_MAIN", "STYLE", "csp", "sha256_source"]
