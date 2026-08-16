# 更新日志

本文件记录 IPClick 的重要变更。版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.4.0] - 2026-08-16

Web 管理端从"能看能改"变成"能装能修"：布局大改版 + 明暗主题、可以就地装卸可选
组件、节点保存即生效、加了连通性与鉴权的就地探测。另修掉一个会让日志静默写错
地方的确凿 bug。

### 破坏性变更

- **`[LOG].output` 填目录时的行为变了（修 bug）。** 以 `/` 结尾、或指向一个已存在
  的目录时，现在会在**该目录内**生成 `ipclick.log`。0.3 会把最后一段整个改写成同级
  文件：`output = "logs/"` 实际写到了 `logs.log`，而 `logs/` 里空空如也，且不报任何
  错。日志是排障的基础设施，在这里静默配错的代价远大于别处。填明确文件名
  （`logs/app.log`）的行为不变。
- **`dashboard_extras()` 的 `engines` 键改名 `components`**，且不再受
  `[BROWSER].enabled` 影响。它现在覆盖全部**五个** extras——0.3 那张表只有四个
  "渲染引擎"，niquests 是纯 HTTP 适配器，完全没有展示位。只影响直接调用该内部
  接口的代码。
- **`browser_engines` 里三个模块级常量（`_playwright_api` / `_patchright_api` /
  `_camoufox_new_browser`）改成懒加载**，`NIQUESTS_AVAILABLE` /
  `DRISSIONPAGE_AVAILABLE` 变成每次求值的模块属性。`from ... import
  NIQUESTS_AVAILABLE` 仍然能用，但拿到的是一个快照；新代码请调 `is_available()`。
- **`ipclick.web.templates.render_test()` 的 `adapters` 参数改成分组结构
  `choices`**，`render_config()` / `render_nodes()` 多了几个关键字参数。

### 新增

- **运行时装 / 卸可选组件**（`/components`）。0.3 刻意不给这个能力（"装依赖要在
  机器上执行命令，那是网页最不该有的能力"），0.4 明确推翻——但只限 IPClick 自己
  声明的那五个 extras：
  - 包名走**白名单常量**，命令以列表交给 `subprocess`（`shell=False`），绝不拼接
    用户输入；
  - **绑定当前解释器**，`pip` 与 `uv pip` 两条路自动探测（uv 建的 venv 默认不装
    pip，这个组合真实存在），两者都没有时明确报错并给出手动命令；
  - 装的是从**本机 ipclick 元数据**读出来的依赖列表，而不是 `ipclick[extra]`——
    后者会把 ipclick 自己拖进解析，要么被升级覆盖掉，要么因为该版本不在索引上而
    直接失败（本地开发版必然如此）；
  - 长任务后台跑 + 页面轮询（`camoufox fetch` 要下约 1 GB）；
  - 错误原样透出（权限失败时那条 `Permission denied` 本身就是答案）；
  - **卸载只卸 Python 包**，浏览器本体不动，界面把它的路径和体积摆出来。
- **安装状态不重启也能刷新。** 探测从 `try: import X` 换成
  `importlib.util.find_spec()` + `invalidate_caches()`：不执行模块代码、能立刻看到
  新装的包，而且**能正确反映卸载**（真 import 过的模块留在 `sys.modules` 里，
  删掉磁盘上的包也不会让它消失）。页面上有手动「刷新状态」按钮，装/卸完成后也会
  自动刷一次，并同步适配器注册表——新装的 niquests 立刻就能选。
- **集群节点保存即生效**（P0-1）。`ForwardingTaskService` 加了 `reload_cluster()`，
  原地替换 `ClusterConfig` 与 `NodePool`；按 id 复用已有的 `NodeState`（直接重建会
  把健康计数清零，那样"连续 N 次才切状态"的判定永远达不到，熔断与恢复双双失效），
  并关掉指向已移除 / 已改地址节点的 channel。0.3 里这两个对象的生命周期等于进程
  生命周期，所以页面上只能写"改完需要重启才生效"——那句提示已经删掉。
- **节点「测试连接」**（`/nodes`）。新增 `Ping` RPC（走鉴权、不做任何业务动作），
  配合免鉴权的 `grpc.health.v1` 探两层，于是能**区分**"连不上"和"连上了但鉴权
  不通过"——这两种的排查方向完全相反。对端还自报是否启用了鉴权，所以"我的令牌对"
  和"它根本不验"也分得开。对着 0.3 的节点调会收到 `UNIMPLEMENTED`，那恰恰证明
  鉴权是通的，页面会如实这么说而不是误报成鉴权失败。
- **「试一试」可以点名目标节点。** 跳过负载均衡直连指定的那一台，用来验证新加的
  机器配对没有——按策略选就只能靠轮询碰运气命中，节点一多完全没法用。刻意走内部
  路径而不改协议：点名是诊断能力，不该变成正式的路由语义。
- **「试一试」支持粘贴 curl。** DevTools 里「复制为 cURL」直接粘进来自动填表。
  认不出的参数会**明确列出来**——静默丢掉一个 `-F` 比不支持它更糟。
- **一键生成凭据**（`/config`）。鉴权令牌 / Web 密码 / 集群共享密钥各可生成一个随机
  值，**只显示一次**（服务端不保存、不写进任何文件，取完即弃），并区分"本机独有"
  与"必须复制到所有其他节点的 `.env`"——共享密钥每台各自生成一个就全对不上了。
- **`{port}` 占位符**（`[TRACE].sqlite_path`、`[LOG].output`）。替换成**运行时实际
  生效**的端口（`--port` 覆盖之后的那个）。同一目录起多个实例时，这两项不岔开会
  **静默**写同一个库、抢同一个日志文件——不报错、不提示。新模板默认带占位符，已有
  部署里写死的路径一个字符都不会被改（自动加后缀会让旧数据看起来像丢了）。
  真撞上了还会打一条明确告警。
- **`ipclick run --web-port`**：gRPC 端口一直有 `--port`，Web 端此前没有，同目录起
  多实例只能靠改配置文件绕。
- **Web 端布局大改版 + 三态主题切换**。左侧导航 + 主内容 + 右侧常驻状态栏的 CSS
  Grid（0.3 是单栏纵向堆叠，总览页所有东西挤在一条竖线上只能一路往下滚）；
  跟随系统 / 亮 / 暗三态，记在 `localStorage`。请求流改成**局部刷新**：
  `<meta refresh>` 每 3 秒重载整页会丢滚动位置、冲掉正在填的过滤条件、页面白闪，
  现在只换那一块的 HTML，且仍由服务端渲染（不在 JS 里复制一份渲染逻辑）。
- **可选组件按「HTTP 适配器 / 浏览器渲染」分类，且没装的也列出来**（置灰 + 安装
  命令）。0.3 会把没装的直接从下拉框里隐藏，对着文档看的人会觉得实现对不上；
  通用占位值 `browser` 也和真实组件名混排，看起来像第六个 extra。

### 修复

- **`[LOG].output` 填目录被静默改写成同级文件**（见破坏性变更）。
- **「试一试」点名失败时不再抛一坨 `_InactiveRpcError` 的 repr**，改成
  "UNAUTHENTICATED：… 两端的 `IPCLICK_CLUSTER_SECRET` 必须完全一致"这种能直接
  照着查的话。诊断页面上的报错尤其不该让人自己找重点。

### 性能

- **批量（`SendBatch`）从 O(N²) 降到 O(N)。** 旧实现每提交一个任务就把**全部**在途
  future 扫一遍找完成的，一千个任务就是五十万次 `done()` 调用，全压在推任务的那个
  线程上，反而拖慢了提交本身。改成 `add_done_callback` + 队列，两边都是 O(1)。
- **批量线程池改为共用。** 旧实现每次 `SendBatch` 都新建再销毁最多
  `[SERVER].max_workers` 个线程（默认 100）；批量本来就是"高频、每次很多任务"的
  用法，这份创建开销是白付的。并发度仍受 `max_workers` 约束。
- **浏览器本体探测加缓存**（随安装状态一起失效）。每次探测都是实打实的文件系统
  扫描（`ms-playwright` 要扫两层），而 0.4 的总览每 5 秒局部刷新一次——不缓存就是
  每 5 秒把那几个目录再翻一遍，而结论只有装 / 卸的时候才会变。
- **总览的自动刷新只算它真正用到的数据**，不再每 5 秒重建一次完整快照（那里面有
  TLS 配置解析、集群拓扑、四个引擎的文件系统探测）。

### 依赖

- `typing-extensions` 下限提到 `>=4.16.0`；`niquests` `>=3.21.0`、
  `patchright` `>=1.61.2`、`drissionpage` `>=4.1.1`、`camoufox` `>=0.5.4`。
- `playwright` 下限只提到 `>=1.60.0` 而不是最新的 1.62：**camoufox 0.5.4 自己钉着
  `playwright<1.61`**，提到 1.62 会让 `[camoufox]` 和 `[playwright]` 直接互斥
  （解析器报 unsatisfiable）。而 camoufox 是 Linux 上的默认引擎，为了追一个更新的
  playwright 把它挤掉不划算。
- 协议核查结论：依赖全是 MIT / BSD-3-Clause / Apache-2.0 / PSF-2.0，没有任何
  copyleft，**IPClick 维持 MIT 不变**。（`drissionpage` 的自定义非开源协议这一轮
  暂缓处理，风险未消失。）

## [0.3.0] - 2026-08-13

集群从"客户端分发"扩展成也支持"服务端转发"，Prometheus 换成内置链路记录，
Web 管理端从只读看板变成可配置、可试请求、能实时看流量的管理界面。

### 破坏性变更

- **移除 `[metrics]` / `[redis]` / `[browser]` / `[all]` 四个 extras。**
  - `[metrics]`（Prometheus）→ 内置链路记录，见下。它按设计不保留单条记录，
    回答不了"我刚才那个请求为什么 403"，而这正是这个库的主要使用场景。
  - `[redis]`（跨节点共享限流）→ 不再需要中间件。服务端转发模式下所有任务都从
    入口节点进来，在那一台上算出来的就是全局额度。`backend = "redis"` 现在直接
    报错而不是静默降级。
  - `[browser]` → 更名 `[playwright]`（它装的就是 playwright，名字该说实话）。
  - `[all]` → 四个浏览器内核全装是 70+ 个包和上 G 的浏览器本体，而一台机器只会
    用其中一个。改为提供 `[win]`（curl_cffi + DrissionPage）与
    `[linux]`（curl_cffi + camoufox + patchright）。
- **协议：`ReqTask.data` 从 `string` 改成 `bytes`**（编号 8 不变）。proto3 的
  `string` 必须是合法 UTF-8，用它装二进制体（图片、gzip、非 UTF-8 表单）会在
  客户端 `json.dumps` 就抛错。线上字节完全相同，旧客户端 → 新服务端无损。
- **协议：移除 `ReqTask.extensions`（编号 18 保留不复用）。** 三个适配器全都不读
  它，是纯死字段。（原设想是远程指定浏览器扩展，但那要求每台机器预放插件目录，
  且 Chromium 系加载扩展必须用持久化 profile，与请求间隔离冲突。）
- **协议：移除 `TaskResp.original_request`（编号 3 保留不复用），换成
  `TaskResp.trace`。** 前者把整个原始请求回传，代理账号密码随之泄漏，响应体积
  还翻倍。后者只带"谁执行的、用了哪个适配器、重试几次、是否经转发、排队多久"，
  不含任何机密。SDK 侧是 `resp.trace`。
- **`DownloadTask` / `Downloader.request()` 移除 `files` 参数。** 协议里从来没有
  这个字段，旧版一律抛 `NotImplementedError`——删掉只是让 API 说实话。要上传文件
  请自己拼 multipart 体走 `data=<bytes>`（`data` 现在是 bytes，任意二进制都能送达）。
- **`impersonate` 传给不支持的适配器现在报错**（`INVALID_ARGUMENT`），
  不再静默忽略。指纹伪装是反爬场景的核心诉求，"我以为开了但没开"比"明确告诉我
  做不到"糟得多。

### 新增

- **服务端转发集群**（`[CLUSTER].forward = "on"`）。调用方只需要知道一个地址，
  入口节点按策略挑节点：挑到自己就本地干，挑到别人就把 `ReqTask` **原样**转过去。
  - **只转一跳**：转发时带 `ipclick-forwarded` metadata，收到带标记的请求一律
    本地执行，环路在协议层面不可能出现。
  - **任意节点都能当入口**：五台机器可以用完全相同的 `ipclick.toml` 和 `.env`，
    只靠 `IPCLICK_CLUSTER_SELF_ID` 区分身份。A 挂了把客户端指向 B 即可。
  - **入口自己也干活**，子节点全挂时它自己兜底执行。
  - **流式不转发**（把每个分片再中转一次会让入口带宽翻倍）。
  - 批量会被摊到多台机器上。
- **集群内部鉴权：一个共享密钥派生出各不相同的令牌。**
  `token = HMAC-SHA256(IPCLICK_CLUSTER_SECRET, "ipclick-node:" + node_id)`。
  所有机器放同一个密钥，每台算出自己的令牌；拿到 B 的令牌不能调 C，也推不出
  共享密钥。加机器只需在节点列表加一行，不用发放新凭据。
- **链路记录与统计**（`[TRACE]`，替代 Prometheus）。两层结构：内存环形缓冲
  （始终开启、默认 500 条、零磁盘）回答"刚才发生了什么"；SQLite（**默认关**、
  30 天保留、WAL）回答"上周三那批任务成功率多少"。写盘走单写线程 + 有界队列，
  队列满了就丢并计数（可观测性数据绝不反压业务），丢弃条数在 Web 端显眼展示。
- **请求压缩**（`[CLIENT].compression`，默认 `auto`）。主要动机是自动化脚本：
  `automation_script` 传的是整个脚本文件，实测 8,625 字节压到 352 字节（24.5×）。
  `auto` 会跳过小请求（压了反而变大）和已压缩的二进制体（实测 60,101 → 60,139）。
- **Web 管理端大改**：
  - **总览**：吞吐、成功率、在途与峰值、状态码分布、各适配器耗时与流量、
    渲染引擎安装状态、集群拓扑、最近请求。
  - **请求流**：3 秒自动刷新，实时看请求打进来；按状态 / 适配器 / URL 过滤；
    目标站点排行与按天趋势。
  - **试一试**：填个网址就地发一次请求，看链路信息与返回的源码。走的是本进程
    `TaskService` 的同一条路径——SSRF 准入、限流、集群转发全都照常生效。
  - **配置**：白名单内的行为配置可改，**写回 `ipclick.toml`**（定点文本替换，
    保留注释与格式，写前留 `.bak`，写入用临时文件 + `os.replace`）。
  - **节点**：集群节点的增删改，同样写回 toml。
  - 刻意**不可改**：`[SECURITY]` 全部、Web 登录凭据、集群密钥与节点 token、
    `[BROWSER].allow_scripts`。刻意**不装东西**：引擎安装状态只展示 + 给命令。
  - 页面里一行 JavaScript 都没有（自动刷新用 `<meta refresh>`，
    CSP 是 `default-src 'none'`）。
- **`ResponseTrace`**：SDK 侧的 `resp.trace.node_id / adapter / attempts /
  forwarded / queued_ms`。
- **`Response.attempts`**：适配器实际发起了几次请求。

### 修复

- **`grpc.so_reuseport` 现在显式关闭。** gRPC 默认开着它（为了多进程分片同一
  端口），后果是端口撞了也能"启动成功"：两个进程都在监听，请求被内核随机分给
  其中一个。症状是"改了配置只有一半生效"、"日志只看到一半请求"，极难定位。
  本项目不提供多进程分片，撞端口现在直接起不来。
- **`automation_script` 写错不再重试。** 它是在页面里执行的 JavaScript；语法错
  或引用了不存在的变量，重试多少次都是同一个结果——默认配置下一个拼错的脚本要先
  起三次浏览器、睡够 15 秒才返回，且最终报成 `-1`（看起来像网络故障）。现在直接
  报 `INVALID_ARGUMENT`，实测从 15.9 秒变成 0.2 秒。
- **`automation_script` 的写法在两套引擎间统一。** DrissionPage 的 `run_js` 要求
  `return x`，Playwright 的 `evaluate` 遇到顶层 `return` 直接
  `SyntaxError: Illegal return statement`。现在 `return x` / 单个表达式 /
  箭头函数三种写法在两边都能用。
- **`[LOG].format` 不再是死配置。** 它此前从未被读取。现在生效，并且会识别出
  标准库 `%(asctime)s` 那套写法——底层是 loguru，占位符是 `{time}`，写错了的
  症状是每行日志都变成一串字面量，所以显式拒绝并告警而不是照用。
- **单机部署不再打集群相关的告警。** 没有节点列表时"本节点将只转发、不执行任务"
  那句话是错的，本节点会照常执行所有任务。
- **「试一试」的适配器下拉框补上 `browser`。** 它是"由服务端决定引擎"的通用写法，
  不在适配器注册表里（请求时才解析），但恰恰是最常用的写法。
- **浏览器引擎的安装状态检查分成两级，且不再可能触发下载。** 三个相关问题：
  1. `is_available()` 原来只查 Python 包能不能 import，于是
     `pip install "ipclick[camoufox]"` 但没跑 `camoufox fetch` 的机器上，
     `config-info` 与 Web 端都显示"可用"，而第一次用会卡几分钟。
     现在分成「Python 包」与「浏览器本体」两级，Web 端也是两列。
  2. camoufox 的 `AsyncNewBrowser` 不传 `executable_path` 时会自己解析路径，
     而它的解析器默认 `download_if_missing=True` —— **缺本体就当场开始下载**
     （本机实测 2 分钟下了 440 MB，总量约 1 GB）。那一刻已经在 gRPC 的请求处理
     线程上：请求必然超时，超时返回后下载还在后台跑，并发的多个首请求还可能各自
     触发一次。现在本项目自己解析路径并显式传进去，结构上排除这条路。
  3. 更隐蔽的一条：连**查状态**都不能用 camoufox 的 `launch_path()` ——
     它内部同样走 `download_if_missing=True`，光渲染一次 Web 端总览页就够触发
     下载。现在改用 `camoufox_path(download_if_missing=False)` 自己拼路径，
     并加了 AST 级护栏测试禁止再出现对 `launch_path()` 的调用。
  另外：适配器**构造**时只查 Python 包（一次 import 判定，不碰文件系统），
  浏览器本体的检查放在 `launch()` 里 —— 那是真正要用浏览器的前一步，
  在那里查才有意义，也不会让"只测参数解析"的代码路径依赖一个真实的浏览器安装。
- **配置页不再出现空白输入框。** 「页面加载超时」原本指向了不存在的
  `[BROWSER].page_timeout`（真实位置是 `[BROWSER.timeout].page_load`），
  「流式分片大小」在配置模板里本来就没有这一项。空白框的危险在于用户一点保存
  就把空值写进配置文件，等于悄悄改了行为。现在每一项都有兜底默认值，
  并加了护栏测试：白名单里的每一项都必须能在随包默认配置里取到值。

### 修复（浏览器渲染「卡死」那一轮排查）

用户报「Web 端用 browser 适配器测试会卡死」。实测一次点击耗时 **296 秒**
（子节点日志 `completed in 296123ms`）。根因不是内存不够——内存只是触发器，
代码里有几层放大器把它放大了二十倍。逐条：

- **`adapter="browser"` 与 `adapter="playwright"` 各建一个适配器实例，各起一个
  chromium。** `TaskService` 的适配器缓存用**请求里写的名字**做键，而
  `get_adapter` 是在内部才把 `browser` 解析成具体引擎。集群里 3 个节点就是
  6 个浏览器进程。现在缓存前先解析。
- **单次渲染的预算无条件加 60 秒脚本超时**，即使请求根本没带
  `automation_script`。调用方填 30 秒、实际单次能挂 150 秒。现在按这次请求
  真正会做的事算：脚本超时只在有脚本时加，冷启动余量只在浏览器还没起来时加。
- **`AdapterError` 会被重试装饰器重试。** 它的含义是"本服务端做不到"（依赖没装、
  浏览器本体没下、渲染被关掉、浏览器超时），重试改变不了其中任何一条，却把
  150 秒变成 600 秒。现在和 `ValidationError` 一样直接上抛。
- **转发的截止时间按 HTTP 请求的口径算**（`timeout × (重试+1) + 15`），对浏览器
  请求必然先超时。入口超时后把节点摘掉、换一台重发，而被放弃的那台**并不停工**，
  继续跑到自己的预算结束——实测入口 135 秒放弃后子节点又白跑了 161 秒，
  三份重复渲染同时压在一台机器上。现在转发超时会按适配器类型覆盖子节点的真实预算。
- **超时会把健康节点标成 unhealthy。** 一个慢请求只说明这一个请求慢。摘掉之后
  流量全压到剩下的机器上、让它们也开始超时，是能把集群推倒的正反馈。现在只有
  `UNAVAILABLE`（连都连不上）才越过后台探活直接摘除。
- **浏览器进程死掉后不重建。** `_ensure_browser` 只判 `is not None`，于是 chromium
  被 OOM killer 干掉之后，该节点的浏览器适配器永久失效（"重启进程才好"）。
  现在用 `is_connected()` 判活并重建，信号量跟着一起重建。
- **调用方断开后照样开工。** 用户关掉标签页之后渲染还会跑满预算并占着页面额度。
  现在开工前检查一次。
- **「试一试」继承了服务端的生产重试策略**，一次点击变成 4 次完整请求。诊断要看的
  是第一次失败的真实原因，现在显式 `max_retries = 0`。
- **「试一试」POST 之后直接渲染结果**，用户按 F5 会把整次请求重新提交一遍——而
  这一页的一次提交可能是几十秒的真实渲染。改成 Post/Redirect/Get。
- **「试一试」对"正在跑"零反馈**（页面里没有 JavaScript，也没有任何文字说明），
  用户必然重复点击，每多点一次就多一份真实渲染。现在页面直接写清楚。
- **`page.evaluate` 的脚本超时被误报成整体预算耗尽。** Python 3.11 起
  `asyncio.wait_for` 超时抛的也是内建 `TimeoutError`，会一路传到外层被吞掉，
  报出错误的秒数、把排查方向指偏。现在用 `future.done()` 区分。
- **`automation_config.wait_for_timeout` 没有上界**，一个请求就能死占一个页面额度
  直到预算耗尽。现在封顶 60 秒。
- **Web 服务器的 `handle_error` 没有覆盖**，客户端提前断开时完整堆栈（含服务端源码
  路径）直接打到 stderr，绕过日志配置——而那恰恰是排查慢请求时最需要干净日志的
  时刻。注意它是 `socketserver.BaseServer` 的方法，不是 handler 的。
- **看板每次刷新都做一次两万行的 Python 侧聚合。** 请求流页 3 秒刷新一次，
  20 万行时 `stats()` 要近 1 秒（`daily` 597ms + `top_hosts` 219ms + `summary` 173ms），
  三个节点就是一整个核。两处改动：目标站点排行改成 SQL 侧 `GROUP BY`（host 现在
  入库时就存一列，与限流器的 `host_of` 是同一套定义——以前查询时现算的那套
  在端口和 IPv6 上和它对不齐），跨天聚合加 10 秒 TTL 缓存（它们描述的是 30 天窗口，
  "3 秒新"没有意义）。实测 `stats()` 240ms → 0.9ms。老库会自动补列。
- **压缩启发式在随机数据上是掷硬币。** 判定阈值是 10%，而均匀随机字节里控制字符的
  期望占比是 10.9%——正好压线，实测 400 次里约 5% 判错，而随机/加密数据恰恰是最
  不该压的。现在主判据改成"采样能否解成合法 UTF-8"（512 字节随机数据凑成合法
  UTF-8 的概率约等于 0），阈值降到 2%。实测 500/500 稳定，文本零误判。

### 修复（发版前的收尾评审）

- **`browser_started` 与 `_ensure_browser` 的判活口径不一致。** 前者只看
  `is not None`，后者用 `is_connected()`。浏览器被 OOM killer 杀掉之后，预算计算
  这边认为"已经起来了"→ 不给冷启动余量，而 `_ensure_browser` 那边判定失联、
  正在重建 —— 这次请求要付完整的冷启动代价却只拿到热路径的预算，必然超时。
  而这恰好是内存最紧张、最需要它成功的时候。两处现在用同一个判据。
- **渲染预算漏算 `wait_for_selector`。** 它是导航之后的第二段等待，用的也是
  `page_timeout`，最坏情况要两份。漏掉的后果是选择器一直等不到的请求先撞上外层
  预算，报成"浏览器任务超过 N 秒未返回"——把排查方向从"选择器写错了"引到
  "浏览器是不是卡了"。
- **`automation_config` 里的数值项写错会漏一个裸 `ValueError` 出去。**
  `{"wait_for_timeout": "abc"}` 抛的是 Python 内建异常而不是 `ValidationError`，
  调用方看到的是一句内部错误。现在统一报参数错误，并显式拒绝 NaN / ±inf
  （它们过得了 `float()`，但 `int(inf)` 会 `OverflowError`）。
- **`_BrowserWorker.close()` 的两步关闭没有各自 try。** 浏览器关不掉（进程已经
  僵了）就不会再去停 driver，playwright 的 node 子进程留下来，反复重启服务就是
  一堆孤儿进程。
- **链路记录写盘失败后写线程仍在空转。** `_write` 置了 `failed` 就返回，但 `_run`
  照样继续取下一批送进同一个必败的写入，把积压的记录逐批变成重复的错误日志。
  现在失败即退出——`submit()` 那边已经不再入队，这个线程没有活可干了。
- **老库回填 `host` 列时把"只存了 host"的行填成 `-`。** 那些行是
  `record_url = false` 时写的，`url` 列里存的**已经是** host，只是没有 `://`。
  按原样保留，与 `TraceRecord.host` 的兜底一致——否则关掉 `record_url` 的部署
  升级之后整个目标站点排行是一片 `-`。
- 注释与类型的准确性：`auto_vacuum` PRAGMA 只对新建库生效（已有库改不了，
  `incremental_vacuum` 也就成了空操作）现在写进注释；`_migrate` 的文档不再声称
  "不做重写"而实际带一次全表回填；`retry()` 的返回类型写全，被装饰的方法保住
  `-> Response`；`browser_engines.__all__` 补上 `engine_status` / `EngineStatus` /
  `package_installed` / `browser_ready`。

### 移除

- **`httpx` 适配器**。它和 niquests 能力重叠（后者还多支持 HTTP/3），维护两套等价的
  HTTP 适配器不划算。枚举值 `HTTPX = 1` 在 proto 里保留并标 `deprecated`，编号不复用；
  旧客户端发来时会拿到一句明确的「已移除，请改用 niquests」，而不是含糊的报错。
  顺带修了措辞：`requests` 被移除后走的是"缺依赖"那张表，打出来是「适配器 'requests'
  **需要额外依赖**：requests 适配器已移除」，自相矛盾。现在拆成两张表。
- `ipclick/metrics.py`、`ipclick/limiter_redis.py` 及对应测试。
- `[MONITOR].metrics_enabled` / `metrics_port` / `metrics_host`。
- `[DOWNLOADER.rate_limit]` 下的 `backend` / `redis_*` 全部键。
- `IPCLICK_REDIS_URL` 环境变量（新增 `IPCLICK_CLUSTER_SECRET`、
  `IPCLICK_CLUSTER_SELF_ID`）。
- 开发依赖 `fakeredis`、可选依赖 `prometheus-client` / `redis`。

## [0.2.4] - 未发布

P1–P6 六个阶段的开发成果。P1 让存量配置真正生效，P2 补安全与可运维，
P3 扩展传输能力，P4 做集群，P5 补齐适配器，P6 加限流与可插拔浏览器引擎。

> 其中的 Prometheus 指标、Redis 分布式限流、`requests` 适配器已在 0.3.0 移除，
> 详见上面那一节。

### 新增

- **令牌鉴权**（P2-1）：gRPC 拦截器校验 `authorization: Bearer <token>`，
  常量时间比较，支持多令牌轮换与环境变量注入。健康检查端点免鉴权。
- **标准健康检查**（P2-2）：实现 `grpc.health.v1`，K8s 探针与服务网格开箱即用。
  优雅下线时先置 `NOT_SERVING` 再停服务，让流量有机会先摘走。
- **Prometheus 指标**（P2-3）：请求量 / 延迟 / 重试 / 拒绝等。`prometheus-client`
  为可选依赖，未安装时所有埋点降级为无操作。标签基数受测试约束，
  不允许出现 URL、主机名这类无界标签。
- **真流式下载**（P3-1）：新增 server-streaming RPC `SendStream`，
  服务端与客户端都不再把整个响应体驻留内存。
- **批量请求**（P3-2）：新增 bidi-streaming RPC `SendBatch`，结果按完成顺序返回。
- **异步客户端**（P3-3）：`ipclick.aio.AsyncDownloader`，基于 `grpc.aio`，
  接口与同步版对应。
- **集群客户端**（P4）：`ipclick.cluster.ClusterDownloader`，多节点负载均衡
  （轮询 / 随机 / 加权）、基于 `grpc.health.v1` 的健康探测、请求级故障转移。
  摘除与恢复都用连续计数阈值，避免一次抖动就让流量反复横跳。
- **只读集群状态页**（P4）：`StatusPageServer` 提供 HTML 页面与 `/api/nodes`
  JSON，仅标准库实现。刻意做成只读，且默认只监听 `127.0.0.1`。
- **`requests` 适配器**（P5-1）：可选依赖 `ipclick[requests]`。（0.3.0 已移除，被 niquests 取代）
- **`playwright` 浏览器渲染适配器**（P5-2）：可选依赖 `ipclick[browser]`（0.3.0 更名 `[playwright]`）。
  起真实浏览器执行 JS 并返回渲染后的 DOM，支持等待选择器、滚动懒加载、整页截图、
  资源类型拦截。`[BROWSER]` 配置节至此第一次真正有消费方。

### 变更

- **`[BROWSER]` 配置节重写**。原来那一节写的插件目录、`.crx`/`.dll` 列表、
  缓存上限 MB 数、`sandbox.level = strict/moderate/off` 没有任何一项有消费方，
  也没有任何一项能落到 playwright 上，全部删除。现在只保留能真正生效的键。
- **参数校验错误不再被重试装饰器吞成 `-1` 响应**。`ValidationError` 现在直接
  上抛：重试多少次都是同样的结果，默认配置下还要先睡满 1+2+4 秒；
  伪装成网络失败也会误导调用方去查网络。`TaskService` 那边本来就会把它映射成
  `INVALID_ARGUMENT`，只是此前根本没机会看到。
  受影响的行为：适配器收到不支持的 HTTP 方法时由"返回 `status_code == -1`
  的响应"改为"抛 `ValidationError`"。
- **服务端错误按类型翻译回客户端**，不再一律吞成 `status_code == -1`：
  - `INVALID_ARGUMENT` → `ValidationError`（参数写错了，改调用代码）
  - `FAILED_PRECONDITION` → `AdapterError`（这个服务端做不到，改部署）

  此前两者都变成 `TransportError`，再被 `request()` 吞成 `-1` 响应，错误信息写着
  "gRPC 调用失败"——调用方会去排查网络。客户端**本地**发现的参数错误早就是直接
  抛出的，服务端发现的没理由不一致：同一个错误不该因为"谁先发现"而表现不同。
  真正的传输失败（连不上、超时）仍然返回 `-1` 响应，行为不变。
- 服务端相应地把 `AdapterError` 从 `INVALID_ARGUMENT` 拆到 `FAILED_PRECONDITION`。
  "适配器不存在 / 依赖没装 / 浏览器渲染被关掉"不是调用方参数写错，
  报成 `INVALID_ARGUMENT` 会让人去改自己的参数，而实际要改的是服务端部署。
- `get_adapter()` 新增可选参数 `browser_settings`，供浏览器适配器读取 `[BROWSER]`。
- 缺可选依赖时，`get_adapter()` 的报错从笼统的"尚未支持"改为给出安装命令——
  "没装"和"没实现"的处理方式完全不同。
- CI 增加 `playwright install --with-deps chromium`，让浏览器渲染用例在 CI 上
  真的跑起来，而不是静默 skip。

### 新增（P8）

- **Web 管理端**（`ipclick run --web` / `-w`，或 `[WEB].enabled`）：带登录的网页
  界面，展示服务端信息、安全配置、限流与浏览器引擎、集群节点健康状态，并支持
  手动摘除 / 恢复节点（仅运行时，重启即复原）。仅用标准库实现。
  未配置密码时**随机生成并打印到控制台**，不设默认口令。
  会话 cookie 带 HttpOnly + SameSite=Strict，写操作校验 CSRF，登录失败按来源 IP
  限速，密码常量时间比对，默认只监听 127.0.0.1。
  刻意**不提供**改配置的能力——这个服务能代任意 URL 发请求，能改它配置的网页
  就是极高价值的目标。

- **`ipclick init`**：一次生成 `ipclick.toml` + `.env`。`.env` 用 600 权限创建、
  预填随机 Web 密码、自动追加进 `.gitignore`，已存在时拒绝覆盖。
- **`ipclick config-info` 显示每项机密的来源**（环境变量 / 配置文件 / 未配置）。
- **加载 `.env` 时检查权限**，同组或其他用户可读会告警。
- **`ipclick --example` / `-e`**：输出模板到 stdout，可直接重定向成文件。
  `-e`（或 `-e toml`）出配置模板，`-e env` 出 `.env` 模板。
  `.env` 模板从 `ENV_OVERRIDES` 表生成，不会出现『模板里有但其实不生效』；
  所有值留空，整份复制过去不改变任何行为。
- **`.env` 支持**：当前工作目录的 `.env` 会被加载进环境变量。
  **不覆盖**已存在的环境变量——容器编排 / CI / systemd 注入的必须能压过仓库里
  那个用于本地开发的 `.env`。自己实现了四十行解析器而不是引 python-dotenv，
  以维持轻量安装。
- **环境变量覆盖集中成一张表**（`ENV_OVERRIDES`），新增 `IPCLICK_MAX_WORKERS`、
  `IPCLICK_MODE`、`IPCLICK_LOG_LEVEL`。此前散在各处 `os.getenv`，
  "到底哪些环境变量有用"只能靠翻代码。有测试盯着表里每一项都真能生效。

### 破坏性变更（P8）

- **机密从 `ipclick.toml` 移到 `.env` / 环境变量**。随包配置里不再有
  `[SECURITY].auth_token`、`[WEB].username/password`、`[PROXY].auth_key/auth_password`。
  正规位置改为 `IPCLICK_AUTH_TOKEN` / `IPCLICK_WEB_USER` / `IPCLICK_WEB_PASSWORD` /
  `IPCLICK_PROXY_AUTH_KEY` / `IPCLICK_PROXY_AUTH_PASSWORD` / `IPCLICK_REDIS_URL`。

  **写在配置文件里仍然照常生效**（不砸现有部署），但启动时会被点名——
  `ipclick.toml` 通常要进版本库，机密会跟着进 git、备份、CI 日志。
  `[SECURITY].allow_secrets_in_config = true` 可关掉提醒。两边都写时环境变量优先。
- **`.env` 模板只剩机密（6 项）**。部署参数（`IPCLICK_HOST` 等）仍然支持但移出模板，
  它们属于容器编排注入的范畴。

- **`httpx` 从核心依赖改为可选**（`pip install "ipclick[httpx]"`）。
  `pip install ipclick` 现在只带 curl_cffi（默认适配器，也是唯一有指纹伪装的），
  依赖从 24 个包降到 17 个。升级后如果用了 `adapter="httpx"` 又没装 extra，
  会收到明确的 `AdapterError` 与安装命令，不会静默换成别的适配器。
- **移除 `requests` 适配器，由 `niquests` 取代**。niquests 是 requests 的
  drop-in 替代，API 完全一致，但底层是 urllib3-future，支持 HTTP/2 与 HTTP/3。
  `adapter="requests"` 会报错并给出迁移提示；protobuf 枚举值 `REQUESTS = 2`
  保留不复用（0.2.3 已发布，旧客户端仍可能发来这个值）。

### 新增（P7）

- **TLS / mTLS**（`[SECURITY.tls]`）：链路加密与双向证书认证。四个建连点全覆盖
  ——同步客户端、异步客户端、服务端绑定、集群健康探活。默认关闭以兼容旧部署，
  监听非回环地址却没开时会打显著告警。
  `require_client_cert` 却不配 `ca_file` 是硬错误，不静默降级。
- **集群节点发现**（`[CLUSTER.discovery]`）：除静态配置外支持 DNS 发现，
  解析出的每条 A/AAAA 记录即一个节点，随探活定期重解析。刷新时按地址复用已有
  节点的健康状态；DNS 解析失败沿用上一次结果，不会把集群摘空。
- **分布式限流**（`[DOWNLOADER.rate_limit].backend = "redis"`）：让整个集群共用
  一份 per-host 额度，而不是每个进程各算各的。并发名额与令牌桶都用 Lua 保证原子性；
  持有者带 TTL，进程崩了名额能自动收回；Redis 故障时放行而不是拒绝所有请求。
- **断点续传**（`ipclick.resume`）：`download_to_file()` / `iter_resumable()`，
  中断后用 HTTP Range 接着下。带 `If-Range` 校验，资源变了就丢掉重来而不是把两个
  版本拼在一起；服务端不支持 Range 时退化成整体重下。

- **客户端重试**（`[CLIENT]`）：客户端到服务端这一跳失败时重试。只重试
  `UNAVAILABLE`（连接没建起来、请求没到过服务端）；`DEADLINE_EXCEEDED` 刻意不
  重试，避免重复执行已经在服务端跑起来的请求。
- **`[GENERAL].mode` 终于有消费方**：`ipclick.create_client()` 按它返回单机或
  集群客户端。`mode = "cluster"` 却没配节点会直接报错，不静默退回单机。

### 修复（P7）

- **全局 `downloader` / `get_downloader()` 无视 `[GENERAL].mode`**。它们硬编码
  单机 `Downloader`，于是配了 `mode = "cluster"` 的人只要用
  `from ipclick import downloader` 就会静默拿到单机客户端——所有流量打在一个
  节点上、没有故障转移，而 `create_client()` 那边却明确拒绝这种静默降级。
  同一个配置项在两条路径上表现不同，比不支持还糟。
  这几个函数随之从 `sdk` 移到 `factory`（否则会形成
  `sdk -> factory -> cluster -> sdk` 的导入环），公开路径不变。
- **`ipclick config-info` 看不到这一轮加的任何配置**。它是用来确认"配置真的
  生效了吗"的命令，却不显示 TLS、鉴权、限流、浏览器引擎、运行模式——而这些
  恰恰是配错了不会报错、只会悄悄少一层防护的那些。现在都展示了（令牌与代理
  密码仍然只说有无、不打印内容），并去掉了"集群尚未实现"这类过期标注。

### 新增（P6）

- **按 host 的并发与 QPS 限制**（`[DOWNLOADER.concurrency]` /
  `[DOWNLOADER.rate_limit]`）。并发用信号量（硬上限），速率用令牌桶（允许突发），
  都按 host 独立计数，默认关闭。对 `Send` / `SendStream` / `SendBatch` 都生效；
  流式请求的额度持有到整条流结束。空闲 host 条目会回收——爬虫会碰到无穷多域名，
  只增不减就是一条稳定的内存泄漏。
  超时抛 `HostLimitTimeout`（gRPC `RESOURCE_EXHAUSTED`），不伪装成网络故障。
  ⚠️ 服务端是一请求一线程，排队会占 worker 线程，README 已写明取舍。
- **浏览器引擎可插拔**，新增三个引擎，共四个：
  - `camoufox`（Firefox，自带完整指纹伪装）
  - `patchright`（Chromium，Playwright 的反检测分支）
  - `playwright`（原版）
  - `DrissionPage`（CDP 直连本机 Chrome）

  前三个都产出 `playwright.async_api.Browser`，共用同一套线程模型、上下文隔离与
  资源拦截；DrissionPage 是另一套 API，单独实现但对外契约一致。
- **`[BROWSER].engine` 与平台默认**。`auto`（默认）在 Windows 上选 DrissionPage、
  在 Linux/macOS 上选 Camoufox。新增 protobuf 枚举 `CAMOUFOX` / `PATCHRIGHT` /
  `BROWSER`，其中 `BROWSER` 表示"渲染就行，引擎由服务端定"。

### 修复（P6）

- **camoufox 下不再强行覆盖 viewport / User-Agent**。它自己生成一整套自洽指纹，
  再盖一层只会自相矛盾，反而比不伪装更容易被识别。
- **DrissionPage 的按请求代理改为报错而非静默忽略**。它的代理是浏览器进程级的，
  启动后改不了；默默忽略等于让请求从错误的出口 IP 发出去。

### 修复

- **`playwright` 启动级代理占位值会让所有直连请求失败**。playwright 文档里
  `proxy={"server": "per-context"}` 的写法是给旧版 chromium 的；现在每个 context
  单独设代理已能直接生效，而设了那个占位值之后，没配代理的 context 会去连一个
  叫 `per-context` 的代理，于是全部 `ERR_PROXY_CONNECTION_FAILED`。
  （开发期发现，未随任何版本发布。）

## [0.2.3] - 2026-08-10

**首个发布到 PyPI 的 0.2.x 版本。** 0.2.0 / 0.2.1 / 0.2.2 均未上传 PyPI，
其标签与 GitHub Release 已删除，全部内容并入本版。

### 新增

- 自动发布流水线 `.github/workflows/release.yml`：推送 `v*` 标签即触发
  lint / 类型检查 / 测试 / 构建 / 产物校验，通过后停在 `pypi` 环境等待人工审批，
  批准后上传 PyPI 并自动创建 GitHub Release。
- 采用 PyPI **Trusted Publishing（OIDC）**，仓库中不存放任何 API token。
- 发布前自动校验：标签与 `pyproject.toml` 版本号必须一致；wheel 必须包含
  `py.typed` 与默认配置；不得混入 tests/`__pycache__`；README 中不得出现
  相对链接（会成为 PyPI 项目页正文，且上传后不可修改）。

### 包含 0.2.0 – 0.2.2 的全部内容

以下条目原属未发布到 PyPI 的 0.2.0 / 0.2.1 / 0.2.2，一并随本版发布。

## [0.2.2] - 2026-08-07（未发布到 PyPI）

本版修复的三项都会被永久固化进 PyPI 页面或分发包，因此在上传前先行修正。

### 修复

- **`stream=True` 在默认适配器上静默丢弃整个响应体**。curl_cffi 在
  `stream=True` 时返回未消费的流式响应，随后读取 `.content` 得到 `b''`，
  而 `status_code` 仍是 200、`exception` 仍是 `None` —— 调用方完全无从察觉。
  服务端本就要把响应体整个塞进一条 protobuf 消息，没有真正的流式通路，
  故与 httpx 适配器保持一致：忽略该参数（README「尚未实现」已如此声明）。
  该问题自 0.1.3 起就存在。
- **`DownloadResponse.raise_for_status()` 抛的异常类型与文档不符**。实际抛基类
  `IPClickError`，而 README 文档写的是 `RequestError`；后者是前者的**子类**，
  于是按文档写 `except RequestError:` 的代码根本捕获不到。改为抛 `RequestError`。
- **随包分发的默认配置预置了作者本机的代理地址**（`127.0.0.1:7890`，Clash 默认端口）。
  这会让所有用户的 `proxy=True` 指向他们自己机器的该端口 —— 可能是完全不相干的服务。
  改为留空，此时 `proxy=True` 会打警告并直连。
- README 中 `LICENSE` 的相对链接改为绝对地址。README 会作为 `long_description`
  打进 wheel 的 METADATA，即 PyPI 项目页正文；PyPI 独立渲染该页面，相对链接会 404，
  且版本一经上传不可修改。

## [0.2.1] - 2026-08-07（未发布到 PyPI）

### 修复

- **CI 类型检查失败**：`basedpyright` 在只有 warning、没有 error 时退出码同样是 1，
  所以 0.2.0 的 CI 在 74 个 warning 上失败。现已把 warning 清零：
  - 补齐 `@override` 标注、类属性类型标注、缺失的参数类型标注；
  - 移除已失效的 `# pyright: ignore` 注释；
  - 可选依赖改用 `Any` 声明 + 私有别名导入，不再需要逐处忽略；
  - 在 `[tool.pyright]` 中显式关闭那些仅因第三方库缺类型标注而必然触发的规则
    （`reportUnknown*`），并说明理由。
- **CI protobuf 一致性检查失败**：ruff 的 `extend-exclude` 用了
  `src/ipclick/dto/proto/task_pb2*.py`，该 glob 要求文件名以 `.py` 结尾，
  匹配不到 `task_pb2.pyi`。于是 `ruff format` 会改写生成的 stub，导致每次重新
  生成都产生差异。现已同时排除 `*.pyi`。
- `ipclick config-info` 不再打印 `[DOWNLOADER]` 的 `connect_timeout` /
  `download_timeout` —— 这两项当前没有消费方，展示出来会让人误以为改了就生效。
  改为展示真正生效的 `[LOG]` 配置；集群节点数标注为"尚未实现"。

### 文档

- README 新增「尚未实现 / 已知限制」章节，逐条列出当前**不生效**的配置节
  （`[GENERAL]` `[CLUSTER]` `[DOWNLOADER]` `[BROWSER]` `[MONITOR]`）与未实现的功能
  （集群/负载均衡、服务端鉴权、4 个未实现的适配器、浏览器渲染、流式下载、
  文件上传、批量请求、异步客户端、Cookie 持久化、客户端重试、可观测性）。
- `default_config.toml` 在上述配置节上加了「⚠️ 尚未实现」标注。
- 新增本 CHANGELOG。

## [0.2.0] - 2026-08-07（未发布到 PyPI）

### 安全

- **修复 SSL 证书校验默认关闭**：SDK 的 `verify` 默认值是 `None`，protobuf 视其为
  "未设置"，服务端因 proto3 隐式默认值收到 `false`。给 `verify_ssl` /
  `timeout_seconds` / `allow_redirects` / `max_retries` / `retry_backoff_seconds` /
  `stream` 加上 `optional` 显式存在性，服务端改用 `HasField` 区分"未设置"与
  "显式设为 0/false"。
- 新增服务端目标 URL 准入策略（SSRF 防护）：协议白名单、云元数据地址拦截、
  可选内网拦截，通过 `[SECURITY]` 配置。
- 响应不再回传 `original_request`（其中含代理账号密码）。
- 启动日志不再 dump 整份配置（含代理密码、`db_uri`）。
- 适配器默认不再信任环境变量中的代理。libcurl 会自行读取 `http_proxy`，
  `Session(trust_env=False)` 拦不住，须显式传空字符串；httpx 默认
  `trust_env=True` 同样会捡起 `ALL_PROXY`。

### 修复

- `server.py` 用 `port` 计算 `max_workers`：`ipclick run --port 9527` 会创建
  9527 个线程的线程池。
- `Downloader.delete()` 不转发 `url`，调用必抛 `TypeError`。
- httpx 适配器：传了 httpx 0.28 已移除的 `proxies=`（走代理必崩）；把 `kwargs`
  JSON 字符串当 dict 索引；`json` / `files` / `allow_redirects` 被静默丢弃；
  回退到不存在的 `self.user_agent`。
- curl_cffi 适配器对空 `kwargs` 无条件 `json.loads` 导致 `JSONDecodeError`。
- 适配器缓存只读不写，每个请求都新建适配器和连接，`cleanup()` 关不掉任何东西。
- `cleanup()` 清空全局 `ADAPTER_CLASSES`，清理一次后进程内再也造不出适配器。
- 优雅停机是假的：`server.stop()` 返回 Event 但没 `wait` 就 `sys.exit`。
- 重试：`max_retries=0` / `retry_delay=0` 被 falsy 判断当作未传；退避最长 600 秒
  占住 worker 线程；重试日志挂在适配器并不存在的 `logger` 属性上，全程静默。
- `request()` 吞异常后返回 `None`，与 `-> DownloadResponse` 的签名不符。
- `DownloadResponse.from_response()` 漏传必填字段，调用必崩。
- `adapter_type` 返回 protobuf 枚举整数而非适配器名称。
- `config-info` 读取小写配置键，永远取不到值。
- `SecureUtil.md5` 在循环中误用 `isinstance(data, ...)` 而非 `isinstance(_d, ...)`。

### 新增

- 公共 API 导出 `HttpMethod` / `DownloadTask` / `DownloadResponse` / `Response`
  及异常类型（README 首个示例原本因 `HttpMethod` 未导出而 `ImportError`）。
- 新增 `exceptions.py` 异常层次，取代到处 `raise Exception`。
- 新增 `py.typed`。
- `Downloader` 支持 `close()` 与上下文管理器；复用 gRPC channel。
- 适配器改用 Session/Client 连接池；服务端补 `maximum_concurrent_rpcs`。
- `register_adapter()` 支持注册自定义适配器。
- `[LOG]` 配置节现在真正生效。
- 新增 proto 生成脚本，固化此前手工修改生成代码导入语句的步骤。

### 变更

- 适配器名/枚举值无法识别时报错，不再静默退回 `curl_cffi`。
- `log_util` 不再在 import 时清空宿主应用的 loguru handler。
- 依赖升级：curl-cffi 0.14→0.16、grpcio 1.76→1.83、protobuf 6→7、click 8.3→8.4、
  python-box 7.3→7.4、uuid-utils 0.14→0.17、ruff 0.14→0.16。移除未使用的
  pydantic，补上漏声明的 typing-extensions。

### 工程

- 启用 ruff 规则集（原本 `select` 全被注释，只能查出 4 个问题）。
- basedpyright 类型错误 52 → 0。
- 从零补齐测试套件，覆盖率 82%，含真实 gRPC 端到端与本地 HTTP 服务测试。
- 新增 GitHub Actions CI：3 个 Python 版本的 lint/format/类型/测试、wheel 内容
  校验、Docker 构建与冒烟。
- Dockerfile 改为多阶段构建、非 root 运行、带 healthcheck；`.dockerignore` 移到
  仓库根目录（放在 `docker/` 下 Docker 根本不会读取）。

## [0.1.3] 及更早

见 git 提交历史。
