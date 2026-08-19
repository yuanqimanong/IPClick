# IPClick

![IPClick Logo](https://raw.githubusercontent.com/yuanqimanong/IPClick/master/docs/logo.png)

> IPClick 名字灵感来源于动画《Link Click》（时光代理人）。正如时光代理人穿梭于不同的时空执行任务，IPClick 帮助您将 HTTP 请求分发到不同的节点高效执行。

IPClick 是一个轻量级、高性能的分布式 HTTP 请求代理工具，基于 gRPC 协议构建。它提供了统一的请求接口，支持多种 HTTP
客户端适配器，帮助开发者更高效地处理网络请求。

> 📚 **完整文档在 [Wiki](https://github.com/yuanqimanong/IPClick/wiki)**。本页只讲"这是什么、怎么跑起来"，
> 深入用法都在 Wiki 里。

| | |
|---|---|
| [安装](https://github.com/yuanqimanong/IPClick/wiki/Installation) | extras、可选组件、两级安装 |
| [快速开始](https://github.com/yuanqimanong/IPClick/wiki/Quick-Start) | 从零到第一个请求 |
| [SDK 用法](https://github.com/yuanqimanong/IPClick/wiki/SDK-Usage) | GET/POST、请求头、代理、流式、断点续传、批量、异步 |
| [命令行](https://github.com/yuanqimanong/IPClick/wiki/CLI) | 全部命令与选项，`--json` 契约与退出码 |
| [配置体系](https://github.com/yuanqimanong/IPClick/wiki/Configuration) | 优先级、环境变量、每一节的含义 |
| [适配器](https://github.com/yuanqimanong/IPClick/wiki/Adapters) | 六个适配器怎么选 |
| [浏览器渲染](https://github.com/yuanqimanong/IPClick/wiki/Browser-Rendering) | 四个引擎、反检测、两级安装 |
| [集群](https://github.com/yuanqimanong/IPClick/wiki/Cluster) | 转发与客户端分发、节点鉴权、部署材料 |
| [Web 管理端](https://github.com/yuanqimanong/IPClick/wiki/Web-Console) | 每个页面能干什么 |
| [安全](https://github.com/yuanqimanong/IPClick/wiki/Security) | 令牌、TLS/mTLS、SSRF 准入 |
| [性能与容量](https://github.com/yuanqimanong/IPClick/wiki/Performance) | 线程、连接池、按 host 限流 |
| [链路记录](https://github.com/yuanqimanong/IPClick/wiki/Observability) | 记了什么、怎么查 |
| [故障排查](https://github.com/yuanqimanong/IPClick/wiki/Troubleshooting) | 连不上、被拦、装不上 |
| [API 参考](https://github.com/yuanqimanong/IPClick/wiki/API-Reference) | 类型与签名 |

## ✨ 特性

- **多适配器**：默认 `curl_cffi`（带 TLS 指纹伪装），按需加装 `niquests`（HTTP/3）
  或四个浏览器引擎之一；也可注册自定义适配器
- **轻量安装**：`pip install ipclick` 只带 curl_cffi，其余走 extras
- **浏览器渲染**：起真实浏览器执行 JS，camoufox / patchright / playwright /
  DrissionPage 四选一，默认按平台挑
- **集群与故障转移**：轮询 / 随机 / 加权均衡、健康探测、自动换节点；服务端转发与
  客户端分发两种形态
- **按 host 限流**：按目标域名限制并发与 QPS，不会把单个站点打爆
- **流式下载 · 断点续传 · 批量 · 异步客户端**
- **令牌鉴权 · TLS/mTLS · SSRF 准入 · `grpc.health.v1` 健康检查**
- **链路记录**：每个请求记一条（谁执行的、重试几次、排队多久），内存环形缓冲 +
  可选 SQLite，零第三方依赖
- **Web 管理端**：带登录的界面——总览、实时请求流、就地发请求看源码、配置改完写回
  `ipclick.toml`、集群节点增删、可选组件装卸。前端零构建链路
- **AI 可调用**：一组结构化输出、退出码分类的命令，外加随包分发的技能包
- **完整类型标注**：随包提供 `py.typed`

## 📦 安装

```bash
pip install ipclick                     # 只带 curl_cffi，17 个包
pip install "ipclick[niquests]"         # 加 HTTP/2 + HTTP/3
pip install "ipclick[camoufox]"         # 加浏览器渲染（还需下载浏览器本体）
```

**没有 `[all]`**：四个浏览器内核全装是 70+ 个包和上 G 的浏览器本体，绝大多数部署
只需要其中一个。装哪个、怎么下浏览器本体，见 [安装](https://github.com/yuanqimanong/IPClick/wiki/Installation)。

需要 Python 3.11+。

## 🚀 快速开始

### 启动服务端

> ⚠️ **0.5.0 换了两个默认端口**：Web 管理端 `9530 → 9527`，gRPC 服务端 `9527 → 9528`。
> 从 0.4 升上来的部署，请在 `ipclick.toml` 的 `[SERVER].port` 里把你原来的端口显式写
> 出来；否则客户端仍然连 9527 时会连到 Web 端口上（那是个 HTTP 服务，gRPC 握手会失败
> 并报一个和端口毫无关系的错误——所以连不上时会附一句端口提示）。

```bash
# 使用默认配置启动
ipclick run

# 指定端口和地址
ipclick run --host 0.0.0.0 --port 9528

# 使用自定义配置文件（TOML 格式）
ipclick run --config /path/to/ipclick.toml

# 显示详细日志
ipclick run --verbose

# 同一目录起第二个实例：gRPC 与 Web 两个端口都要岔开
ipclick run --port 9528 --web-port 9531 -w
```

### 吞吐上不去？先加进程，不是加线程

服务端是一请求一线程的 CPython 进程，**GIL 才是吞吐天花板**。实测 16 核机器上
单进程只能用出 1.5 个核，把 `[SERVER].max_workers` 从 32 调到 256 对吞吐
**没有任何影响**（279.8 / 275.9 / 277.9 QPS）。

0.6.0 起可以起多个工作进程，靠 SO_REUSEPORT 共享同一个端口——分发由内核做，
不需要中间件，对调用方完全透明（仍然只有一个地址一个端口）：

```toml
[SERVER]
processes = 4        # 0 = 按 CPU 核数自动（上限 8）；1 = 单进程（默认）
```

并发上去之后大面积失败、而服务端 CPU 却很空闲，是另一件事——那是准入上限：

```toml
[SERVER]
max_concurrent_rpcs = 0    # 0 = max_workers × 8。这一项决定"排队能排多长"，
                           # 和 max_workers（"同时能干多少活"）是两件事
```

实测并发 100 时：单进程 196.6 → 331.1 QPS，四进程 → 544.6 QPS；
1000 并发的成功率从 25.8%（异步客户端）回到 100%。详见 CHANGELOG 的 0.6.0 一节。

0.7.0 起还可以把服务端换成协程（实验性，默认关）：

```toml
[SERVER]
async_mode = true    # grpc.aio + 协程，不再一请求一线程。实测端到端 1.8×
```

**协程和多进程不是二选一**：协程解决并发模型，多进程解决多核，单进程再优化
也只能用一个核。两者叠加才是终态。默认关的理由见 CHANGELOG 的 0.7.0 一节。

初始化配置（推荐）：

```bash
ipclick init
```

一次生成两份文件：`ipclick.toml`（行为配置）和 `.env`（机密）。`.env` 用 **600**
权限创建、预填一个随机 Web 密码、自动追加进 `.gitignore`；文件已存在时拒绝覆盖
（要覆盖加 `--force`）。

也可以只要模板：

```bash
ipclick -e > ipclick.toml        # 配置模板（-e 等价于 -e toml）
ipclick -e env > .env            # 机密模板（注意自己 chmod 600）
```

### 什么放哪儿

| | 放什么 | 进版本库？ |
|---|---|---|
| **`ipclick.toml`** | 行为配置：超时、重试、限流、浏览器引擎、SSRF 策略 | ✅ 应该 |
| **`.env`** | **只放机密**：令牌、密码、带凭据的连接串 | ❌ 绝不 |

`.env` 里只有这 6 项：

```
IPCLICK_AUTH_TOKEN            gRPC 鉴权令牌
IPCLICK_WEB_USER              Web 管理端用户名
IPCLICK_WEB_PASSWORD          Web 管理端密码
IPCLICK_PROXY_AUTH_KEY        代理账号
IPCLICK_PROXY_AUTH_PASSWORD   代理密码
IPCLICK_CLUSTER_SECRET        集群共享密钥（所有节点一致；每台机器的令牌由它派生）
```

**机密写在 `ipclick.toml` 里仍然生效**——受信环境里图省事是合理的。只是启动时会
点名提醒（`ipclick.toml` 通常要进版本库，机密会跟着进 git、备份、CI 日志）。
确实想这么放就设 `[SECURITY].allow_secrets_in_config = true` 关掉提醒。
两边都写时**环境变量优先**。

`ipclick config-info` 会逐项显示每个机密**来自哪里**（环境变量 / 配置文件 / 未配置）
——配错了地方是最难自己发现的一类问题。

查看当前**实际生效**的配置——TLS、鉴权、限流、浏览器引擎、运行模式一目了然：

```bash
ipclick config-info
```

### 客户端使用

```python
from ipclick import Downloader, HttpMethod

# 创建下载器实例（建议用 with，退出时自动关闭 gRPC 连接）
with Downloader() as downloader:
    # 发送 GET 请求
    response = downloader.get("https://httpbin.org/ip")
    print(response.text)
    print(response.json())

    # 发送带参数的请求
    response = downloader.request(
        method=HttpMethod.GET,
        url="https://httpbin.org/get",
        headers={"User-Agent": "IPClick/1.0"},
        params={"key": "value"},
        timeout=30,
    )

    # 检查请求是否成功
    if response.is_success():
        print(f"请求成功，耗时: {response.elapsed_ms}ms")
    else:
        print(f"请求失败: {response.error}")
```

`request()` 及各便捷方法不会因为网络问题抛异常，失败时返回 `status_code == -1`
且 `error` 非空的 `DownloadResponse`。参数本身非法（如 URL 为空）会抛 `ValidationError`。

更多用法——代理、流式、断点续传、批量、异步、指定适配器——见
[SDK 用法](https://github.com/yuanqimanong/IPClick/wiki/SDK-Usage)。

## 🖥️ Web 管理端

```bash
ipclick run --web        # 或 -w
```

启动时会把访问地址和登录信息打到控制台：

```
==============================================================
  IPClick Web 管理端: http://127.0.0.1:9527/
  用户名: admin
  密码:   N8mSKkdPbpiGzB128At3

  ⚠️ 该密码为本次启动随机生成，重启后失效。
==============================================================
```

**没配密码就随机生成**，而不是给个 admin/admin 之类的默认值——默认弱口令是这类
管理界面被打穿的头号原因。要固定下来用环境变量（推荐，密码不该进版本库）：

```bash
IPCLICK_WEB_USER=ops IPCLICK_WEB_PASSWORD=... ipclick run -w
```

或写进 `.env`，或配 `[WEB].username` / `password`。

### 六个页面

**左侧导航 + 主内容 + 右侧状态栏**的 CSS Grid，左下角**亮 / 暗**主题切换
（记在 `localStorage`，刷新不丢）。

| 页面 | 干什么 |
|---|---|
| **总览** `/` | 吞吐、成功率、在途与峰值、状态码分布、各适配器耗时与流量、集群拓扑、最近请求；右栏常驻服务端 / 安全 / 链路 / 限流 / 组件状态，**gRPC 与 Web 两个端口分别列出**（取运行时实际值） |
| **请求流** `/trace` | **实时**看请求打进来，刷新频率可选 1 秒 / 5 秒 / 30 秒或关闭。按状态 / 适配器 / URL 过滤，看目标站点排行与按天趋势 |
| **试一试** `/test` | 填一个网址（或直接**粘一条 curl**）就地发一次请求，看链路信息与返回的**源码**。参数与 SDK 的 `request()` 一一对应；可**点名**打到某个节点 |
| **组件** `/components` | 五个可选 extras 的两级安装状态，可以就地**安装 / 卸载 / 下载浏览器本体**（带真进度条）。可以**点名装到集群里某台机器**——省掉逐台 SSH |
| **配置** `/config` | 两个分页：**基础设置**（端口、线程、超时、日志…）与**集群设置**（转发开关、节点增删、子节点部署材料）。写回 `ipclick.toml`，可一键**生成凭据** |
| **AI 接入** `/skill` | 给 AI 代理用的技能包：装它的命令、全文、以及 `SKILL.md` 下载 |

> `/nodes` 已并进 `/config?tab=cluster`——节点管理本来就是集群配置的一部分。老地址会自动跳过去。

页面上的**时间一律按你浏览器所在时区显示**。服务端跑在 UTC 的容器里时，
东八区的人看到的仍是自己的钟点。

「试一试」走的是本进程 `TaskService` 的**同一条**代码路径——SSRF 准入、限流、
以及开了转发时的分发全都照常生效，请求也会像真实请求一样出现在请求流里。

**装到集群里某台机器**：组件页顶部有个「装到哪台机器」下拉，和「试一试」是同一个
心智模型。对端跑的是**同一个** `InstallManager`——同一份白名单、同一条命令规划、
同一个"一次只跑一个任务"的约束。**默认关闭**，要被操作的那台自己打开
`[CLUSTER].allow_remote_install`：它等于允许调用方在那台机器上跑 pip，
是从"能代发 HTTP 请求"到"能改本机 Python 环境"的实质提权。

> **一个例外，说在前面**：Web 管理端「生成部署材料」产出的子节点 `ipclick.toml`
> 里，这一项是**打开**的——理由是那份配置本来就由主控生成，你已经信任它了，
> 否则逐台 SSH 上去装适配器很烦。不想要就把生成出来的那一行改成 `false`，
> 之后主控的组件页对这台会返回"未开启"。

**从 curl 导入**：浏览器 DevTools 里对着请求「复制为 cURL」，粘进输入框就能自动
填好 URL / 方法 / 请求头 / 请求体。认不出的参数会明确列出来，不静默丢弃。

**指定目标节点**：配了 `[CLUSTER].nodes` 就有一个"目标节点"下拉，选中后强制打到
那一台。开了服务端转发时走转发器（跳过负载均衡），**没开转发时由这一页直连**那台
机器发一次 gRPC（用集群内部令牌）。

**测试连接**：集群设置里每张节点卡片一个按钮，只验**连通性**与**集群内部鉴权**，
不发业务请求。失败时区分"连不上"（查进程和网络）和"鉴权不通过"（核对各节点 `.env` 里的
`IPCLICK_CLUSTER_SECRET`）——这两种的排查方向完全相反。

**生成凭据**：`/config` 可以为鉴权令牌、Web 密码、集群共享密钥各生成一个随机值，
**只显示一次**（服务端不保存、不写进任何文件，取完即弃），并明确区分"本机独有"
和"必须复制到所有其他节点的 `.env`"——集群共享密钥每台各自生成一个就全对不上了。

### 能改什么、不能改什么

- **能改**（写回 toml，保留注释与格式，改动前留 `.bak`）：74 项，分 12 组——服务端、
  日志、下载行为、重试、连接池、按 host 限流、浏览器渲染、代理、集群、链路记录、
  Web 管理端、客户端与压缩。
- **不能改**：`[SECURITY]` 全部（令牌、TLS、SSRF 三个开关）、Web 自己的登录凭据、
  集群共享密钥与各节点 token、`[BROWSER].allow_scripts`、`[BROWSER].executable_path`、
  `[CLUSTER].allow_remote_install`。后三项不是"配置"是"授权"——它们能让调用方在
  服务端跑任意东西，不该和超时、线程数摆在同一个表单里让人顺手划过去。
- **只报真正变了的项**：改一个日志级别就只说这一项，不会每次都报"20 项已写回、
  12 项需要重启"——那样人会开始无视这句提示，而它在真需要重启时是唯一的信号。
- **能装依赖**：`/components` 可以装 / 卸那五个 extras，以及下载浏览器本体。
  包名走白名单常量，命令以列表交给 `subprocess`（`shell=False`）并绑定当前解释器；
  `pip` 与 `uv pip` 自动探测。长任务（`camoufox` 要下约 1 GB）跑后台，页面轮询进度。
  **卸载只卸 Python 包**，浏览器本体不动——界面会把它的路径和体积摆出来，让人自己决定。

写回配置是**定点文本替换**：只换被改动那一行等号右边的值，注释与排版原样保留；
写入前会先校验结果仍是合法 TOML，用临时文件 + `os.replace` 落盘。

前端没有构建链路——没有模板引擎、框架、打包工具，也没有任何外部资源。
CSP 的 `script-src` 用的是内联脚本的 **sha256 哈希**而不是 `'unsafe-inline'`，
所以注入进来的 `<script>` 执行不了。

安全措施：会话 cookie 带 `HttpOnly` + `SameSite=Strict`，所有写操作校验 CSRF
token，登录失败按来源 IP 限速（5 次后锁定 5 分钟，锁定期间正确密码也不放行），
密码用常量时间比对，`X-Frame-Options: DENY` + 严格 CSP。

> ⚠️ 界面本身是**明文 HTTP**。默认只监听 `127.0.0.1`；要远程访问请用 SSH 隧道，
> 或放在做了 TLS 终止的反向代理之后。直接把 `[WEB].host` 改成 `0.0.0.0` 会让
> 登录密码在网络上裸奔（启动时会打告警）。

### 让局域网内的其他设备也能打开

```bash
ipclick run -w --web-lan               # 等价于 --web-host 0.0.0.0
ipclick run -w --web-host 192.168.1.5  # 或者只绑某一张网卡
```

也可以写进配置（`[WEB].host = "0.0.0.0"`），命令行优先。

在可信的局域网里自用是合理的（拿手机看看跑得怎么样），但要清楚代价：

- **明文 HTTP**，登录密码会在网线上裸奔。跨网段暴露请放在做了 TLS 终止的反向代理后面。
- 更要紧的是 **gRPC 那一侧**。管理端的「试一试」能代发任意请求，而同一网段的人绕过
  网页直连 gRPC 端口是更省事的那条路——所以开局域网访问之前先把
  `[SECURITY].auth_token` 配上。启动前和启动时各会告警一次。

### 主题

左下角「亮 / 暗」两态，选择记在 `localStorage`。服务端默认值：

```toml
[WEB]
theme = "light"   # light / dark
```

优先级是**浏览器里点过的那一下 > `[WEB].theme`**。反过来的话，用户每刷新一次页面，
自己刚选的主题就会被配置文件推翻一次。

> 0.5.0 去掉了「跟随系统」。那一档靠 CSS 的 `prefers-color-scheme`，取决于浏览器读不
> 读得到桌面偏好——Linux 上 Chrome/Firefox 要 GTK 或 xdg-desktop-portal 配好才认，
> 读不到就静默按亮色处理。一个在半数机器上不生效、失败时又毫无迹象的选项，比没有这个
> 选项更糟。配置里留着旧的 `auto` 也能启动，按 `light` 处理。

## 🤖 命令行（给人，也给 AI）

除了部署用的 `init` / `run` / `health`，还有一组**结构化输出**的命令，供脚本和 AI 代理调用。
全部支持 `-J/--json`。

```bash
ipclick status  --json              # 服务端在不在、这台机器能用哪些适配器
ipclick fetch   <URL> --json        # 发一次请求
ipclick trace   list -n 20 --json   # 查链路记录
ipclick node    probe --json        # 探集群节点：连得上吗、令牌配对吗
ipclick component list --json       # 五个可选组件的安装状态
ipclick config  show --json         # 生效配置（机密脱敏）
```

### 输出契约

加 `--json` 后，**stdout 上有且只有一个 JSON 文档**，成功失败都是；日志与进度走
stderr。所以 `ipclick ... --json | jq` 永远安全。每个文档都带 `ok` 和 `exit_code`，
和进程退出码一致。

| 退出码 | 含义 | 往哪儿查 |
|---|---|---|
| 0 | 成功 | — |
| 1 | 拿到响应但不理想（HTTP ≥ 400、探测不通、装包失败） | 目标本身 |
| 2 | 命令行参数写错了 | `--help` |
| 3 | 连不上 IPClick **服务端**（请求根本没发出去） | 进程 / 地址端口 / 防火墙 |
| 4 | 鉴权失败 | 令牌（`IPCLICK_AUTH_TOKEN`） |
| 5 | 参数被服务端拒绝，或本地配置不合法 | 调用参数或 `ipclick.toml` |

`status: -1` 表示没拿到 HTTP 响应，这时看 `reached_server`：`false` 是没连上 IPClick
本身（退出码 3），`true` 是 IPClick 正常、连不上**目标站点**（退出码 1，原因在 `error`）。

### 响应体

最容易踩的一处。`--json` 时 `body` 默认截断到 64 KiB，并给出 `body_truncated: true`：

```bash
ipclick fetch <URL> --json --max-body 0     # 不截断
ipclick fetch <URL> -o page.html            # 写文件，不截断
ipclick fetch <URL> > page.html             # 不加 --json：正文进 stdout、元信息进 stderr
```

响应不是合法 UTF-8（图片、压缩流）时 `body_encoding` 是 `base64`。小的完整给出，
超过上限的**给空串**——半截 base64 解不出任何东西，这时用 `-o` 存文件。

### 给 AI 代理装技能包

```bash
ipclick skill install        # 写到 .claude/skills/ipclick/SKILL.md
ipclick skill show           # 打印全文
ipclick skill path           # 只打印路径
```

技能包是一份随 wheel 分发的 Markdown，讲清楚什么时候该用 IPClick、上面那套输出契约、
以及几个最容易踩的坑。装完之后直接对代理说"用 ipclick 抓一下 …"即可，不必再逐条解释。
Web 端的 `/skill` 页也能看全文、复制命令、下载 `SKILL.md`。

升级 IPClick 之后重装一次，用法说明会跟着版本走；已经存在且被改过的副本不会被覆盖，
要覆盖加 `--force`。

## 📂 项目结构

```
IPClick/
├── src/
│   └── ipclick/
│       ├── __init__.py          # 包入口，导出公共 API
│       ├── __main__.py          # 模块入口
│       ├── sdk.py               # 同步 SDK 客户端
│       ├── aio.py               # 异步 SDK 客户端（grpc.aio）
│       ├── server.py            # gRPC 服务端实现
│       ├── auth.py              # 服务端令牌鉴权拦截器
│       ├── health.py            # grpc.health.v1 健康检查
│       ├── trace.py             # 链路记录（内存环形缓冲 + 可选 SQLite）
│       ├── compression.py       # 请求压缩策略（自动化脚本压缩收益最大）
│       ├── components.py        # 五个可选 extras 的清单、两级安装状态、分类
│       ├── skill.py             # 随包分发的 AI 技能包（CLI 与 Web 端共用）
│       ├── ports.py             # 默认端口的唯一事实来源（9528 gRPC / 9527 Web）
│       ├── tls.py               # TLS / mTLS 配置与凭据构造
│       ├── secrets.py           # 机密只从 .env / 环境变量读，不进 toml
│       ├── resume.py            # 断点续传（Range 请求）
│       ├── factory.py           # 按 [GENERAL].mode 选单机或集群客户端
│       ├── limiter.py           # 按 host 的并发与 QPS 闸门
│       ├── exceptions.py        # 异常层次
│       ├── py.typed             # 类型标注标记
│       ├── adapters/            # 下载器适配器
│       │   ├── base.py          # 适配器基类、重试装饰器、脚本与导航错误分类
│       │   ├── settings.py      # [DOWNLOADER] 配置
│       │   ├── browser_settings.py  # [BROWSER] 配置
│       │   ├── browser_engines.py   # 引擎选择、两级安装检测、启动
│       │   ├── curl_cffi_adapter.py # 默认适配器（唯一有指纹伪装的）
│       │   ├── niquests_adapter.py  # HTTP/2 + HTTP/3
│       │   ├── browser_adapter.py   # playwright / patchright / camoufox
│       │   ├── drission_adapter.py  # DrissionPage（CDP 直连）
│       │   └── registry.py      # 适配器注册表（含已移除适配器的指引）
│       ├── cluster/             # 集群
│       │   ├── node.py          # 节点模型与 [CLUSTER] 配置
│       │   ├── balancer.py      # 轮询 / 随机 / 加权
│       │   ├── pool.py          # 节点池与健康探测
│       │   ├── client.py        # ClusterDownloader（客户端分发 + 故障转移）
│       │   ├── forwarder.py     # ForwardingTaskService（服务端转发）
│       │   ├── tokens.py        # 由共享密钥派生每节点独立令牌
│       │   ├── discovery.py     # static / dns 节点发现
│       │   ├── probe.py         # 节点探测：连得上吗、鉴权配对吗
│       │   └── status_page.py   # 只读状态页
│       ├── web/                 # Web 管理端（仅标准库，无前端构建链路）
│       │   ├── server.py        # HTTP 服务、会话、CSRF、路由、CSP
│       │   ├── pages.py         # 各页面的数据与操作
│       │   ├── templates.py     # HTML 渲染（每处插值都要过 esc）
│       │   ├── assets.py        # CSS 与 JS（CSP 用脚本哈希放行）
│       │   ├── installer.py     # 装 / 卸可选组件（白名单 + 后台任务 + 进度解析）
│       │   ├── deploy.py        # 为子节点生成 toml / .env / 启动命令 / zip
│       │   ├── curl_parser.py   # 把 curl 命令解析成「试一试」表单
│       │   ├── editable.py      # 可编辑配置项白名单（按分页归组）
│       │   └── auth.py          # 凭据、会话、登录限速
│       ├── skills/ipclick/      # SKILL.md 本体（随 wheel 分发）
│       ├── cli/                 # 命令行工具
│       │   ├── main.py          # 部署命令：init / run / health / config-info
│       │   ├── agent.py         # 给程序 / AI 调用：fetch / status / trace / node / …
│       │   ├── output.py        # JSON 输出契约与退出码分类
│       │   └── skill_cmd.py     # ipclick skill show / install / path
│       ├── config_loader/       # 配置加载
│       │   ├── loader.py        # 优先级与环境变量覆盖
│       │   ├── dotenv.py        # .env 解析
│       │   ├── placeholders.py  # 路径里的 {port} 占位符
│       │   └── writer.py        # 写回 toml（定点替换，保留注释）
│       ├── configs/             # 默认配置文件
│       ├── dto/                 # 数据传输对象
│       │   ├── models.py        # 数据模型定义
│       │   ├── response.py      # 统一响应对象
│       │   └── proto/           # Protobuf 定义与生成脚本
│       ├── services/            # gRPC 服务实现
│       └── utils/               # 工具模块（日志、配置、URL 安全校验等）
│           └── module_probe.py  # 不执行模块代码的"装没装"探测（find_spec）
├── docker/                      # Docker 相关文件
├── tests/                       # 测试代码
├── pyproject.toml               # 项目配置
└── README.md
```

## 🐳 Docker

```bash
docker build -f docker/Dockerfile -t ipclick:latest .
docker run -d -p 9528:9528 --name ipclick ipclick:latest              # gRPC
docker run -d -p 9528:9528 -p 9527:9527 --name ipclick ipclick:latest  # 再带上 Web 管理端
```

镜像多阶段构建、非 root 运行。详细说明见 [`docker/构建.md`](https://github.com/yuanqimanong/IPClick/blob/master/docker/%E6%9E%84%E5%BB%BA.md)。

## 🚧 已知限制

配置文件里出现、但改了不会有效果的项，这里如实列出。

- **服务发现只支持 `static` 与 `dns`**。etcd / Consul 的原生 API 没接（Consul 可以走它的 DNS 接口）。
- **分块下载与临时存储**未实现。流式通路已经就绪，这两项可以在其上做，目前还没做。
- **集群流式只有建流这一步会故障转移**。流建立之后中途断掉不会自动重连——那需要
  Range 请求才能不重复数据。批量请求整批发给同一个节点，不跨节点拆分。
- **没有文件上传字段**。要发 multipart 就自己拼好请求体，用 `data=<bytes>` 加上
  `Content-Type: multipart/form-data; boundary=...` 发出去。
- **请求之间不共享 cookie jar**，每次请求相互独立。

## 🛠️ 开发

```bash
uv sync --all-groups          # 安装含开发依赖
uv run pytest                 # 运行测试
uv run pytest --cov=ipclick   # 带覆盖率
uv run ruff check src/ tests/ # 代码检查
uv run ruff format src/ tests/# 代码格式化
uv run basedpyright src/      # 类型检查
```

修改 `src/ipclick/dto/proto/task.proto` 后需重新生成代码：

```bash
uv run python src/ipclick/dto/proto/generate.py
```

## 🤝 贡献

欢迎贡献代码、报告问题或提出建议！请通过 [GitHub Issues](https://github.com/yuanqimanong/IPClick/issues)
或 [Pull Requests](https://github.com/yuanqimanong/IPClick/pulls) 参与项目开发。

## 📄 许可证

本项目采用 [MIT License](https://github.com/yuanqimanong/IPClick/blob/master/LICENSE) 开源许可证。

Copyright (c) 2025 元气码农少女酱
