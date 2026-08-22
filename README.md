# IPClick

![IPClick Logo](https://raw.githubusercontent.com/yuanqimanong/IPClick/master/.github/logo.png)

> IPClick 名字灵感来源于动画《Link Click》（时光代理人）。正如时光代理人穿梭于不同的时空执行任务，IPClick 帮助您将 HTTP 请求分发到不同的节点高效执行。

IPClick 是一个基于 gRPC 的分布式 HTTP 请求代理。你把请求交给它，它带着浏览器 TLS
指纹、重试策略、限流与准入规则去发，必要时换一台机器的出口 IP 去发，并把每一次
执行都记成一条可查的链路。

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
| [性能与容量](https://github.com/yuanqimanong/IPClick/wiki/Performance) | 进程、线程、连接池、按 host 限流 |
| [链路记录](https://github.com/yuanqimanong/IPClick/wiki/Observability) | 记了什么、怎么查 |
| [故障排查](https://github.com/yuanqimanong/IPClick/wiki/Troubleshooting) | 连不上、被拦、装不上 |
| [示例集](https://github.com/yuanqimanong/IPClick/wiki/Recipes) | 按场景可直接抄的代码 |
| [API 参考](https://github.com/yuanqimanong/IPClick/wiki/API-Reference) | 类型与签名 |
| [开发](https://github.com/yuanqimanong/IPClick/wiki/Development) | 本地环境、门禁、项目结构、发布 |

## ✨ 特性

- **多适配器**：默认 `curl_cffi`（带 TLS 指纹伪装），按需加装 `niquests`（HTTP/3）
  或四个浏览器引擎之一；也可注册自定义适配器
- **轻量安装**：`pip install ipclick` 只带 curl_cffi，其余走 extras
- **浏览器渲染**：起真实浏览器执行 JS，camoufox / patchright / playwright /
  DrissionPage 四选一，默认按平台挑
- **集群与故障转移**：轮询 / 随机 / 加权均衡、健康探测、自动换节点（仅 GET/HEAD/OPTIONS）；服务端转发与
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

```bash
# 使用默认配置启动
ipclick run

# 指定端口和地址。注意 gRPC 端默认已经是 "[::]"（所有网卡，v4 + v6），
# 写 --host 0.0.0.0 不是"打开对外"，而是把监听收窄成只 IPv4。
# 真正默认只听本机的是 Web 管理端
ipclick run --host 0.0.0.0 --port 9628

# 使用自定义配置文件（TOML 格式）
ipclick run --config /path/to/ipclick.toml

# 显示详细日志
ipclick run --verbose

# 同一目录起第二个实例：gRPC 与 Web 两个端口都要岔开
# （9528 / 9527 就是这两者的默认值，所以两个都得改）
ipclick run --port 9628 --web-port 9531 -w
```

默认端口是 **gRPC 9528** 与 **Web 9527**。两者都跑在 HTTP/2 之上但协议不同，
把客户端指到 Web 端口上会在 gRPC 握手阶段失败，报出的错误还和端口毫无关系——
所以连不上时 IPClick 会额外附一句端口提示。

### 吞吐上不去？先加进程，不是加线程

服务端默认一请求一线程，**GIL 才是吞吐天花板**——调大 `[SERVER].max_workers` 对吞吐没有
影响。要用满多核就起多个工作进程，靠 SO_REUSEPORT 共享同一个端口，分发由内核做，对调用方
完全透明（仍然只有一个地址一个端口）：

```toml
[SERVER]
processes = 4            # 0 = 按 CPU 核数自动（上限 8）；1 = 单进程（默认）
max_concurrent_rpcs = 0  # 0 = max_workers × 8。决定"排队能排多长"，
                         # 和 max_workers（"同时能干多少活"）是两件事
async_mode = false       # true = grpc.aio 协程模式（实验性，默认关）
```

**仅 Unix。** Windows 上没有 `os.fork`，会打一条告警后降级成单进程。Web 管理端只在 0 号
进程里起，内存按进程数线性增长——每进程各有一份适配器与连接池，开浏览器渲染时尤其要算。

协程和多进程**不是二选一**：前者解决并发模型，后者解决多核，两者叠加才是终态。

基准数据、按 host 限流与容量规划见 [性能与容量](https://github.com/yuanqimanong/IPClick/wiki/Performance)。
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

`.env` 模板里就这 6 项（`ipclick -e env` 生成的、`config-info` 审计来源的都是这一份）：

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

`request()` 及各便捷方法不会因为网络问题抛异常——传输失败和 DNS 解析失败都返回
`status_code == -1` 且 `error` 非空的 `DownloadResponse`。会抛出来的是这几类：

| 异常 | 什么时候 |
|---|---|
| `ValidationError` | 参数不合法（URL 为空、方法不支持…），**以及重定向超过 10 跳** |
| `URLNotAllowedError` | 入口 URL **或任何一跳重定向目标**被 `[SECURITY]` 拒绝 |
| `AdapterError` | 适配器/浏览器没准备好 |
| `HostLimitTimeout` | 等 per-host 配额超时

更多用法——代理、流式、断点续传、批量、异步、指定适配器——见
[SDK 用法](https://github.com/yuanqimanong/IPClick/wiki/SDK-Usage)。

## 🖥️ Web 管理端

```bash
ipclick run --web        # 或 -w
```

启动时把访问地址、用户名和密码打到控制台。**没配密码就随机生成**（重启失效），而不是给个
admin/admin 之类的默认值——默认弱口令是这类管理界面被打穿的头号原因。要固定下来用环境变量
（推荐，密码不该进版本库），或写进 `.env`，或配 `[WEB].username` / `password`：

```bash
IPCLICK_WEB_USER=ops IPCLICK_WEB_PASSWORD=... ipclick run -w
```

| 页面 | 干什么 |
|---|---|
| **总览** `/` | 吞吐、成功率、状态码分布、各适配器耗时与流量、集群拓扑、最近请求 |
| **请求流** `/trace` | 实时看请求打进来，按状态 / 适配器 / URL 过滤，看站点排行与按天趋势 |
| **试一试** `/test` | 填网址或直接粘一条 curl，就地发一次请求看链路与源码；可点名打到某个节点 |
| **组件** `/components` | 五个可选 extras 的两级安装状态，可就地安装 / 卸载 / 下载浏览器本体 |
| **配置** `/config` | 75 项配置分 12 组，写回 `ipclick.toml`（保留注释、留 `.bak`）；集群节点增删 |
| **AI 接入** `/skill` | 技能包全文、安装命令与 `SKILL.md` 下载 |

「试一试」走的是本进程 `TaskService` 的**同一条**代码路径——SSRF 准入、限流、以及开了转发时
的分发全都照常生效，请求也会像真实请求一样出现在请求流里。

**不能从界面改的**：`[SECURITY]` 全部、Web 自己的登录凭据、集群共享密钥与各节点 token、
`[BROWSER].allow_scripts`、`[BROWSER].executable_path`、`[CLUSTER].allow_remote_install`。
后三项不是"配置"是"授权"——它们能让调用方在服务端跑任意东西，不该和超时、线程数摆在同一个
表单里让人顺手划过去。

> ⚠️ 界面本身是**明文 HTTP**，默认只监听 `127.0.0.1`。要远程访问请用 SSH 隧道，或放在做了
> TLS 终止的反向代理之后。局域网自用可以 `--web-lan`（等价于 `--web-host 0.0.0.0`），但开
> 之前先把 `[SECURITY].auth_token` 配上——同网段的人绕过网页直连 gRPC 端口是更省事的那条路。

每个页面具体能干什么、写回配置的机制、前端的 CSP 与会话设计，见
[Web 管理端](https://github.com/yuanqimanong/IPClick/wiki/Web-Console)。

## 🤖 命令行（给人，也给 AI）

部署那一组是四条：`init` / `run` / `health` / `config-info`，都是给人看的，**不支持
`--json`**。另有一组**结构化输出**的命令供脚本和 AI 代理调用，全部支持 `-J/--json`：

```bash
ipclick status  --json              # 服务端在不在、这台机器能用哪些适配器
ipclick fetch   <URL> --json        # 发一次请求
ipclick trace   list -n 20 --json   # 查链路记录
ipclick node    probe --json        # 探集群节点：连得上吗、令牌配对吗
ipclick component list --json       # 五个可选组件的安装状态
ipclick config  show --json         # 生效配置（机密脱敏）
```

**输出契约**：加 `--json` 后 **stdout 上有且只有一个 JSON 文档**，成功失败都是；日志与进度
走 stderr，所以 `ipclick ... --json | jq` 永远安全。每个文档都带 `ok` 和 `exit_code`，与进程
退出码一致。

| 退出码 | 含义 | 往哪儿查 |
|---|---|---|
| 0 | 成功 | — |
| 1 | 拿到响应但不理想（HTTP ≥ 400、探测不通、装包失败） | 目标本身 |
| 2 | 命令行参数写错了 | `--help` |
| 3 | 连不上 IPClick **服务端**（请求根本没发出去） | 进程 / 地址端口 / 防火墙 |
| 4 | 鉴权失败 | 令牌（`IPCLICK_AUTH_TOKEN`） |
| 5 | 参数被服务端拒绝，或本地配置不合法 | 调用参数或 `ipclick.toml` |

`status` 为 `-1` **或 `null`** 都表示没拿到 HTTP 响应，所以别拿 `d["status"] == -1` 当判据，
先读 `exit_code` / `ok`；要区分方向再看 `reached_server`。

`--json` 时 `body` 默认截断到 **65536 个字符**（不是字节——抓中文页面时字节数会大出两三倍），
并给出 `body_truncated: true`。`--max-body 0` 不截断，`-o <file>` 写文件也不截断。响应不是
合法 UTF-8（图片、压缩流）时 `body_encoding` 为 `base64`，超过上限的给空串——半截 base64 解
不出任何东西。

给 AI 代理装技能包（一份随 wheel 分发的 Markdown，讲清楚何时该用 IPClick、上面这套输出契约、
以及几个最容易踩的坑）：

```bash
ipclick skill install        # 写到 .claude/skills/ipclick/SKILL.md
```

全部命令与选项见 [命令行](https://github.com/yuanqimanong/IPClick/wiki/CLI)。

## 🐳 Docker

```bash
docker build -f docker/Dockerfile -t ipclick:latest .
docker run -d -p 9528:9528 --name ipclick ipclick:latest              # gRPC
# 再带上 Web 管理端：容器里必须显式开，而且要听 0.0.0.0——
# 默认只听 127.0.0.1，那是容器自己的回环，-p 映射进不去
docker run -d -p 9528:9528 -p 127.0.0.1:9527:9527 --name ipclick ipclick:latest \
  ipclick run -w --web-lan
```

镜像基于 **python:3.14-slim**，多阶段构建、非 root 运行，并且**默认自带浏览器渲染
能力**——patchright 和 chromium 本体都打进去了，拉下来直接 `-a patchright` 就能渲染
JS 页面，不用在容器里再下一遍。

不需要渲染就构建精简版，省 1.5 GB：

```bash
docker build -f docker/Dockerfile --build-arg ENGINE=none -t ipclick:slim .
```

`ENGINE` 可选 `patchright`（默认）/ `camoufox` / `linux`（两个都装）/ `none`。
详细说明见 [`docker/构建.md`](https://github.com/yuanqimanong/IPClick/blob/master/docker/%E6%9E%84%E5%BB%BA.md)。

## 🚧 已知限制

配置文件里出现、但改了不会有效果的项，这里如实列出。

- **服务发现只支持 `static` 与 `dns`**。etcd / Consul 的原生 API 没接（Consul 可以走它的
  DNS 接口）。
- **分块下载与临时存储**未实现。流式通路已经就绪，这两项可以在其上做，目前还没做。
- **集群流式只有建流这一步会故障转移**。流建立之后中途断掉不会自动重连——那需要 Range 请求
  才能不重复数据。批量请求整批发给同一个节点，不跨节点拆分。
- **没有文件上传字段**。要发 multipart 就自己拼好请求体，用 `data=<bytes>` 加上
  `Content-Type: multipart/form-data; boundary=...` 发出去。`files=` 不在协议里，进程内直接
  传给适配器时会抛 `ValidationError`。
- **查询参数原样传递。** `params={"start": "2024-01-01"}` 发出去就是 `start=2024-01-01`。
- **两次请求之间不保持会话。** `curl_cffi` / `niquests` 适配器按（代理, 证书校验, 指纹）缓存
  Session，但**每次请求前都会清空这个 session 的 cookie jar**——服务端没有调用方会话的概念，
  共用 session 却保留 cookie 等于跨调用方串号。单次请求内部（含重定向链）的 cookie 传递照常
  由底层库处理。需要保持会话请在请求里显式传 `cookies=`。

## 🛠️ 开发

```bash
uv sync --all-groups            # 安装含开发依赖
uv run pytest                   # 运行测试
uv run ruff check src/ tests/   # 代码检查
uv run ruff format src/ tests/  # 代码格式化
uv run basedpyright src/ tests/ # 类型检查
```

修改 `src/ipclick/dto/proto/task.proto` 后需重新生成代码：

```bash
uv run python src/ipclick/dto/proto/generate.py
```

依赖按用途分组（`test` / `lint` / `proto` / `release`，`dev` 是聚合组），
CI 里每个 job 只装自己那一份。`[camoufox]` 与 `[playwright]` 在
`[tool.uv].conflicts` 里声明为互斥（camoufox 钉着 `playwright<1.61`），
所以装可选适配器时要逐个 `--extra` 列出，不能用 `--all-extras`。

项目结构、门禁清单、提交规范与发布流程见 [开发](https://github.com/yuanqimanong/IPClick/wiki/Development)。

## 🤝 贡献

欢迎贡献代码、报告问题或提出建议！请通过 [GitHub Issues](https://github.com/yuanqimanong/IPClick/issues)
或 [Pull Requests](https://github.com/yuanqimanong/IPClick/pulls) 参与项目开发。

## 📄 许可证

本项目采用 [MIT License](https://github.com/yuanqimanong/IPClick/blob/master/LICENSE) 开源许可证。

Copyright (c) 2025 元气码农少女酱
