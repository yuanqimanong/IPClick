from __future__ import annotations

import base64
import hashlib


STYLE = """
/* ---------- 设计变量 ----------
   亮色是基准，暗色只覆盖变量。**两态**主题：
     data-theme="light"（或未设置）-> 亮
     data-theme="dark"             -> 暗

   0.5 去掉了"跟随系统"。它靠 prefers-color-scheme，而那一位取决于浏览器读不读
   得到桌面偏好——Linux 上 Chrome/Firefox 要 GTK 或 xdg-desktop-portal 配好才认，
   读不到就静默按亮色处理。一个在半数机器上不生效、失败时又毫无迹象的选项，
   比没有这个选项更糟：用户会以为是页面坏了。现在只有明确的两个值。            */
:root {
  color-scheme: light;
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
  --ok: #1a7f37;      --ok-bg: #dafbe1;   --ok-rgb: 26,127,55;
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
  --ok: #3fb950;    --ok-bg: #0f2f18;   --ok-rgb: 63,185,80;
  --bad: #ff7b72;   --bad-bg: #3c1618;
  --warn: #d29922;  --warn-bg: #3a2d10;
  --info: #79c0ff;  --info-bg: #0c2d6b;
  --shadow: 0 1px 2px rgba(1,4,9,.5), 0 3px 8px rgba(1,4,9,.4);
  --shadow-lg: 0 8px 28px rgba(1,4,9,.7);
  color-scheme: dark;
}


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
/* 「需重启」是个**动作**，不是解释——和旁边那句灰字不是一类信息。
   混在同一行里，人扫过去只会看见解释、漏掉动作，然后对着一个"改了没生效"
   的界面排查。所以给它警示色、单独一行。 */
/* 用红而不是琥珀：这一条是"改完必须去做的动作"，要能在一屏十几行里被一眼扫到。
   .bad 在别处表示失败，但徽标自带文字（"需重启"），不会和失败状态混淆。 */
.field-row .pill.restart { margin-top: .3125rem; font-weight: 700; }
.pill.running { margin-top: .3125rem; font-weight: 700; }
.pill.running::before { content: "▶"; margin-right: .125rem; font-size: .625rem; }
.pill.restart::before { content: "⟳"; margin-right: .125rem; }
.actions { display: flex; gap: .625rem; align-items: center; flex-wrap: wrap; margin-top: 1rem; }
.inline-form { display: inline; }
fieldset { border: 1px solid var(--line-soft); border-radius: var(--radius);
           padding: .25rem 1.125rem 1rem; margin: 0 0 1rem; }
legend { font-size: .8125rem; font-weight: 600; padding: 0 .375rem; color: var(--fg-dim); }
.filters { display: flex; gap: .625rem; align-items: flex-end; flex-wrap: wrap; }
.filters > div { flex: 0 0 auto; }
.filters input, .filters select { width: auto; min-width: 8rem; }
.filters .check { display: flex; align-items: center; gap: .375rem; padding-bottom: .4375rem; }

/* 分段选择器：连成一排的 radio。视觉上是一个整体，语义上还是 radiogroup，
   所以键盘方向键、无 JS 提交这两件事都是白送的。 */
.seg { display: inline-flex; border: 1px solid var(--line); border-radius: var(--radius-sm);
       overflow: hidden; background: var(--bg-sunk); }
.seg input { position: absolute; opacity: 0; pointer-events: none; width: 0; height: 0; }
.seg label { margin: 0; padding: .3125rem .625rem; font-size: .75rem; font-weight: 600;
             color: var(--fg-dim); cursor: pointer; user-select: none; white-space: nowrap;
             border-left: 1px solid var(--line-soft); transition: background .12s, color .12s; }
.seg label:first-of-type { border-left: 0; }
.seg label:hover { background: var(--bg-soft); color: var(--fg); }
.seg input:checked + label { background: var(--accent); color: var(--accent-fg); }
/* 键盘走到这里时要看得见——radio 本体是隐藏的，焦点环得画在 label 上。 */
.seg input:focus-visible + label { outline: 2px solid var(--accent); outline-offset: -2px; }

/* 活体指示。没有它的话，页面安静时人分不清是"没有新请求"还是"刷新根本没在跑"。 */
.livebar { display: flex; align-items: center; gap: .4375rem; margin-top: .75rem;
           padding-top: .6875rem; border-top: 1px solid var(--line-soft);
           font-size: .75rem; color: var(--fg-dim); }
.livedot { width: .5rem; height: .5rem; border-radius: 50%; background: var(--ok); flex: 0 0 auto;
           box-shadow: 0 0 0 0 var(--ok); animation: livepulse 2s ease-out infinite; }
.livebar.paused .livedot { background: var(--fg-faint); animation: none; box-shadow: none; }
.livebar.stale .livedot { background: var(--bad); animation: none; box-shadow: none; }
@keyframes livepulse {
  0%   { box-shadow: 0 0 0 0 rgba(var(--ok-rgb),.45); }
  70%  { box-shadow: 0 0 0 .375rem rgba(var(--ok-rgb),0); }
  100% { box-shadow: 0 0 0 0 rgba(var(--ok-rgb),0); }
}
/* 系统设置了"减少动态效果"就别脉动——这个点只是状态提示，不值得违背它。 */
@media (prefers-reduced-motion: reduce) { .livedot { animation: none; } }
.filters .check label { margin: 0; }

/* 行内的复选 / 单选：标签和控件同一行，不占满宽度 */
.check-inline { display: inline-flex; align-items: center; gap: .375rem; margin: 0 1rem .25rem 0;
                color: inherit; font-size: .8125rem; }
.check-inline input { width: auto; }
.check-inline .hint { color: var(--fg-dim); font-size: .75rem; }
.check-row { display: flex; flex-wrap: wrap; gap: .25rem 0; padding-top: .375rem; }
.inline-choice { margin-top: .375rem; }
.two-up { display: grid; grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr)); gap: .5rem; }

/* 「更多参数」折叠区。默认收起，填过东西就自动展开（服务端渲染时加 open）——
   提交后回到页面却看不到自己设过的代理，会让人以为那一项没生效。 */
details.more { border: 1px solid var(--line-soft); border-radius: var(--radius);
               padding: 0 1rem; margin: .75rem 0; background: var(--bg-soft); }
details.more > summary { cursor: pointer; padding: .625rem .25rem; font-size: .875rem; font-weight: 600;
                         list-style: none; display: flex; align-items: center; gap: .5rem; }
details.more > summary::-webkit-details-marker { display: none; }
details.more > summary::before { content: "▸"; color: var(--fg-dim); font-size: .75rem; }
details.more[open] > summary::before { content: "▾"; }
details.more > summary .hint { color: var(--fg-dim); font-weight: 400; font-size: .75rem; }
details.more[open] > summary { border-bottom: 1px solid var(--line-soft); margin-bottom: .5rem; }
details.more .field-row:last-of-type { border-bottom: none; }
details.more > p.note { padding-bottom: .875rem; }

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

/* ---------- 分页标签 ---------- */
.tabs { display: flex; gap: .25rem; border-bottom: 1px solid var(--line-soft); margin-bottom: 1.25rem; }
.tabs a { padding: .5rem .875rem; font-size: .875rem; color: var(--fg-dim); text-decoration: none;
          border-bottom: 2px solid transparent; margin-bottom: -1px; }
.tabs a:hover { color: var(--fg); text-decoration: none; }
.tabs a.on { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }

/* ---------- 节点卡片 ----------
   **一行最多 4 张**，窄了自动降列。0.4 是一张表格，加减机器要在一行里横向找输入框；
   一台一张卡之后，"这台是什么状态、能对它做什么"聚在一起。

   列宽取 max(15rem, 四分之一)：后者把上限钉在 4 列（宽屏上不会摊成 5、6 列，
   那样每张卡都很窄、信息反而更难扫），前者保证窄屏时优先降列而不是压扁卡片。 */
.nodes-grid { display: grid; gap: .75rem;
              grid-template-columns: repeat(auto-fill, minmax(max(15rem, calc(25% - .5625rem)), 1fr)); }
.node-card { border: 1px solid var(--line-soft); border-radius: var(--radius); padding: .875rem 1rem;
             background: var(--bg-elev); display: flex; flex-direction: column; gap: .375rem; }
.node-card .top { display: flex; align-items: center; gap: .5rem; flex-wrap: wrap; margin-bottom: .25rem; }
.node-card .top .nm { font-weight: 600; font-size: .875rem; word-break: break-all; }
.node-card label { font-size: .75rem; margin-bottom: .125rem; }
.node-card input { font-size: .8125rem; padding: .3125rem .5rem; }
.node-card .acts { display: flex; gap: .375rem; flex-wrap: wrap; margin-top: .375rem; }
.node-card .note { font-size: .6875rem; color: var(--fg-faint); margin: 0; }

/* ---------- 弹窗 ----------
   只用 hidden 属性开关，没有 <dialog>：那个元素在几个还在用的浏览器版本里
   行为不一致，而这里要的只是"盖一层、居中一个表单"。                     */
.dialog { position: fixed; inset: 0; z-index: 50; display: grid; place-items: center;
          background: rgba(0,0,0,.45); padding: 1.5rem; }
.dialog[hidden] { display: none; }
.dialog-box { background: var(--bg-elev); border: 1px solid var(--line); border-radius: var(--radius);
              box-shadow: var(--shadow-lg); padding: 1.25rem 1.375rem; width: 100%; max-width: 30rem;
              max-height: 90vh; overflow-y: auto; }

/* ---------- 可复制的命令行 ---------- */
.copy-row { display: flex; gap: .5rem; align-items: flex-start; margin-bottom: .875rem; }
.copy-row pre { flex: 1 1 auto; margin: 0; }
.copy-row button { flex: 0 0 auto; }

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

/* ---------- 安装进度 ----------
   camoufox 的浏览器本体约 1 GB，慢网络下十几分钟。那段时间里"在下载"和"卡死了"
   在页面上长得一模一样，这一组就是为了把它们区分开。
   两种形态：知道百分比时画确定态的条；不知道时画一条来回滑的动画 + 已下载字节数
   与速度——后者不依赖子进程报进度，永远有。                                 */
.prog { margin: .5rem 0 .25rem; }
.prog .track { height: .5rem; border-radius: 2rem; background: var(--bg-sunk); overflow: hidden; }
.prog .fill { height: 100%; background: var(--accent); border-radius: 2rem;
              transition: width .3s ease; }
.prog .fill.indeterminate { width: 35%; animation: slide 1.4s ease-in-out infinite; }
@keyframes slide { 0% { margin-left: -35%; } 100% { margin-left: 100%; } }
.prog .meta { display: flex; gap: .875rem; flex-wrap: wrap; margin-top: .375rem;
              font-size: .75rem; color: var(--fg-dim); }
.prog .meta b { color: var(--fg); font-weight: 600; }
/* 减少动效偏好：把来回滑改成整条低透明度铺满——仍然看得出"在进行中"，
   但不再有持续运动。 */
@media (prefers-reduced-motion: reduce) {
  .prog .fill.indeterminate { animation: none; width: 100%; opacity: .35; margin-left: 0; }
}

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


SCRIPT_BOOT = """
(function () {
  var root = document.documentElement;
  var mode = null;
  try {
    var saved = localStorage.getItem('ipclick-theme');
    if (saved === 'dark' || saved === 'light') mode = saved;
  } catch (e) { /* 隐私模式下 localStorage 会抛，回落到服务端默认值 */ }
  if (mode === null) {
    var fallback = root.getAttribute('data-default-theme');
    mode = fallback === 'dark' ? 'dark' : 'light';
  }
  root.setAttribute('data-theme', mode);
})();
"""

SCRIPT_MAIN = """
(function () {
  'use strict';

  // ---------- 主题：亮 / 暗 ----------
  // 两态，没有"跟随系统"。那一档靠 prefers-color-scheme，而它取决于浏览器读不读
  // 得到桌面偏好——Linux 上常常读不到，于是静默变成亮色，看起来就像功能坏了。
  var KEY = 'ipclick-theme';
  var MODES = { light: 1, dark: 1 };

  function current() {
    try {
      var saved = localStorage.getItem(KEY);
      if (saved && MODES[saved]) return saved;
    } catch (e) { /* 隐私模式 */ }
    return document.documentElement.getAttribute('data-default-theme') === 'dark' ? 'dark' : 'light';
  }

  function apply(mode) {
    document.documentElement.setAttribute('data-theme', mode);
    var buttons = document.querySelectorAll('[data-theme-set]');
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].setAttribute('aria-pressed', String(buttons[i].dataset.themeSet === mode));
    }
  }

  document.addEventListener('click', function (event) {
    var target = event.target.closest ? event.target.closest('[data-theme-set]') : null;
    if (!target) return;
    var mode = target.dataset.themeSet;
    if (!MODES[mode]) return;
    try { localStorage.setItem(KEY, mode); } catch (e) {}
    apply(mode);
  });

  apply(current());

  // ---------- 弹窗 ----------
  // 开 / 关都只切 hidden。Esc 与点遮罩关闭是最低限度的礼貌——一个只能靠那个
  // 小「关闭」按钮退出的弹窗，第一次用的人会以为自己被卡住了。
  document.addEventListener('click', function (event) {
    var opener = event.target.closest ? event.target.closest('[data-dialog]') : null;
    if (opener) {
      var box = document.getElementById(opener.dataset.dialog);
      if (box) {
        box.hidden = false;
        var first = box.querySelector('input:not([type=hidden])');
        if (first) first.focus();
      }
      return;
    }
    var closer = event.target.closest ? event.target.closest('[data-dialog-close]') : null;
    if (closer) {
      var owner = closer.closest('.dialog');
      if (owner) owner.hidden = true;
      return;
    }
    // 点遮罩本身（不是里面的盒子）时关闭
    if (event.target.classList && event.target.classList.contains('dialog')) event.target.hidden = true;
  });
  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Escape') return;
    var open = document.querySelectorAll('.dialog:not([hidden])');
    for (var i = 0; i < open.length; i++) open[i].hidden = true;
  });

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
    // 目标机器跟着页面走：选了子节点时，装 / 卸都发到那台上去。
    post('/api/components/action',
         { op: button.dataset.install, extra: button.dataset.extra, node: activeNode() })
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

  function bytes(n) {
    if (!n) return '0 B';
    var units = ['B', 'KB', 'MB', 'GB'], i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + ' ' + units[i];
  }

  function clock(seconds) {
    var m = Math.floor(seconds / 60), s = seconds % 60;
    return m ? m + 'm' + (s < 10 ? '0' : '') + s + 's' : s + 's';
  }

  // 进度条。知道百分比就画确定态；不知道就画不确定态 + 已下载字节与速度——
  // 后者由服务端的采样线程量目录大小得出，不依赖子进程肯不肯报进度。
  function renderProgress(job) {
    var box = document.getElementById('job-progress');
    if (!box) return;
    var p = job && job.progress;
    if (!job || job.status !== 'running' || !p) { box.hidden = true; return; }
    box.hidden = false;
    var fill = box.querySelector('.fill');
    var meta = box.querySelector('.meta');
    if (p.percent === null || p.percent === undefined) {
      fill.className = 'fill indeterminate';
      fill.style.width = '';
    } else {
      fill.className = 'fill';
      fill.style.width = Math.max(0, Math.min(100, p.percent)) + '%';
    }
    var parts = [];
    if (p.percent !== null && p.percent !== undefined) parts.push('<b>' + p.percent.toFixed(1) + '%</b>');
    if (p.phase) parts.push(esc(p.phase));
    // 本次任务写进磁盘的量（不是目录总量），以及速度。这两项由服务端采样得出，
    // 不依赖子进程肯不肯报进度——子进程一声不吭时，它们就是"没卡死"的唯一证据。
    if (p.done_bytes) parts.push('本次已写入 <b>' + bytes(p.done_bytes) + '</b>');
    if (p.speed) parts.push(bytes(p.speed) + '/s');
    parts.push('已用 ' + clock(job.elapsed || 0));
    // 每项各包一个 span：.meta 是 flex + gap，靠元素间距分隔，
    // 直接拼文本会连成一串（"20.0%已下载 638 MB"）。
    meta.innerHTML = parts.map(function (part) { return '<span>' + part + '</span>'; }).join('');
  }

  // 组件页当前对着哪台机器。空串 = 本机。
  function activeNode() {
    var box = document.getElementById('job-box');
    return (box && box.dataset.activeNode) || '';
  }

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
                        esc(clock(job.elapsed || 0) + ' · ' + job.command) + '</span>';
      renderProgress(job);
      output.textContent = (job.output || []).join('\\n');
      output.scrollTop = output.scrollHeight;
    } else {
      title.innerHTML = '<span class="pill ' + (ok ? 'info' : 'bad') + '">' + esc(message || '') + '</span>';
      renderProgress(null);
    }
  }

  var pollTimer = null;
  function pollJob() {
    if (pollTimer) return;
    pollTimer = setInterval(function () {
      fetch('/api/components/status' + (activeNode() ? '?node=' + encodeURIComponent(activeNode()) : ''),
            { credentials: 'same-origin' })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!data.job) return;
          showJob(data.job, '', true);
          if (data.job.status !== 'running') {
            clearInterval(pollTimer);
            pollTimer = null;
            // 装完/卸完重新加载：安装状态、适配器下拉、注册表全都变了，
            // 局部改几个徽标不如让服务端重新渲染一次准。
            // reload 而不是跳 /components：带着 ?node= 的地址原样保留，
            // 否则装完子节点的组件会莫名其妙跳回本机那一页。
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

  // ---------- 时间按看的人的时区显示 ----------
  // 服务端渲染出来的是**服务端**本地时间。一旦它跑在 UTC 的容器里（Docker 默认），
  // 东八区的人看到的每一条都慢八小时——而且症状很温和："时间看着像那么回事，
  // 就是和自己的表对不上"，很少有人会当成 bug 报出来。
  //
  // <time datetime="..."> 里是带偏移量的 ISO-8601，浏览器据此换算。没有 JS 时
  // 标签里的文字仍是服务端时间，也就是维持旧行为，不会变成空白。
  function localizeTimes(root) {
    var nodes = (root || document).querySelectorAll('time[datetime]');
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      var d = new Date(el.getAttribute('datetime'));
      if (isNaN(d.getTime())) continue;   // 解析不了就别动，留着服务端那份
      el.textContent = d.getFullYear() + '-' + p2(d.getMonth() + 1) + '-' + p2(d.getDate()) +
                       ' ' + p2(d.getHours()) + ':' + p2(d.getMinutes()) + ':' + p2(d.getSeconds());
      if (!el.title) el.title = el.getAttribute('datetime');   // 悬停能看到原始带偏移的值
    }
  }
  function p2(n) { return (n < 10 ? '0' : '') + n; }
  localizeTimes(document);

  // ---------- 实时刷新 ----------
  // 0.3 用的是 <meta refresh> 整页重载：滚动位置丢失、正在填的过滤条件被冲掉、
  // 每 3 秒白闪一次。改成只换那一块的 HTML，服务端仍然负责渲染（不在 JS 里
  // 复制一份渲染逻辑）。
  //
  // 0.5 加了频率切换。切档**不重载整页**：这一页的用途就是盯着看，重载会把
  // 滚动位置和刚展开的错误行一起丢掉。选择用 replaceState 写回地址栏，所以
  // 手动刷新、收藏、复制链接给别人，档位都还在。
  var live = document.querySelector('[data-live-src]');
  if (live) {
    var liveTimer = null;
    var liveMs = 0;
    var liveBar = document.getElementById('live-bar');
    var liveStatus = document.getElementById('live-status');

    function liveWord(ms) {
      return ms === 1000 ? '每秒更新' : '每 ' + (ms / 1000) + ' 秒更新';
    }
    function clock() {
      var d = new Date();
      return ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2) +
             ':' + ('0' + d.getSeconds()).slice(-2);
    }
    function say(text, state) {
      if (liveStatus) liveStatus.textContent = text;
      if (!liveBar) return;
      liveBar.classList.toggle('paused', state === 'paused');
      liveBar.classList.toggle('stale', state === 'stale');
    }

    function pull() {
      if (document.hidden) return;  // 后台标签页不刷，别白占服务端 worker
      fetch(live.dataset.liveSrc, { credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.text() : Promise.reject(r.status); })
        .then(function (html) {
          live.innerHTML = html;
          localizeTimes(live);   // 新换进来的行还是服务端时间，要再转一次
          say(liveWord(liveMs) + ' · 上次 ' + clock(), '');
        })
        .catch(function () {
          // 一次失败无所谓，下一轮再来。但要说出来——否则页面停在旧数据上
          // 一动不动，看起来和"没有新请求"一模一样。
          say('上次更新失败（' + clock() + '），下一轮重试', 'stale');
        });
    }

    function applyInterval(ms) {
      liveMs = ms;
      if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
      if (!ms) { say('实时刷新已关闭', 'paused'); return; }
      say(liveWord(ms), '');
      liveTimer = setInterval(pull, ms);
    }

    applyInterval(Math.max(0, parseInt(live.dataset.liveInterval || '5000', 10) || 0));

    var seg = document.getElementById('live-seg');
    if (seg) {
      seg.addEventListener('change', function (e) {
        var ms = parseInt(e.target.value, 10);
        if (isNaN(ms)) return;
        applyInterval(ms);
        try {
          var url = new URL(window.location.href);
          url.searchParams.set('live', String(ms));
          url.searchParams.set('_', '1');  // 没这个标记的话，服务端认为"没提交过表单"
          window.history.replaceState(null, '', url);
        } catch (err) { /* 地址栏没跟上而已，刷新这件事本身照常 */ }
      });
    }
  }

  function esc(text) {
    return String(text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
})();
"""


def sha256_source(script: str) -> str:
    digest = hashlib.sha256(script.encode("utf-8")).digest()
    return f"'sha256-{base64.b64encode(digest).decode('ascii')}'"


def csp() -> str:
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
