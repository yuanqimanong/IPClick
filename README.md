# IPClick

![IPClick Logo](https://raw.githubusercontent.com/yuanqimanong/IPClick/master/.github/logo.png)

> IPClick 的名字灵感来自《Link Click》（时光代理人）。项目将 HTTP 请求分发到不同节点执行。

IPClick 是一个基于 gRPC 的分布式 HTTP 请求代理。调用方通过 Python SDK、命令行或 Web
管理端提交请求，服务端负责适配器选择、TLS 指纹伪装、重试、限流和 URL 准入。
多机部署时，请求可以由不同节点的出口 IP 发出，执行过程会记录为可查询的链路。

> 📚 **完整文档在 Wiki**。本页介绍安装、启动和常用入口，详细配置与 API 用法见文档目录。[Wiki](https://github.com/yuanqimanong/IPClick/wiki)

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

- **多适配器**。默认使用带 TLS 指纹伪装的 `curl_cffi`，可按需安装 `niquests`（HTTP/3）
  或浏览器引擎，也支持注册自定义适配器
- **按需安装**。`pip install ipclick` 安装核心依赖，可选适配器通过 extras 安装
- **浏览器渲染**。通过 camoufox / patchright / playwright / DrissionPage 执行 JS，
  默认引擎按平台选择
- **集群与故障转移**。支持轮询、随机、加权均衡和健康探测，以及服务端转发与客户端分发。
  仅 GET/HEAD/OPTIONS 请求会自动换节点重试
- **按 host 限流**。按目标域名控制并发与 QPS
- **流式下载 · 断点续传 · 批量 · 异步客户端**
- **令牌鉴权 · TLS/mTLS · SSRF 准入 · `grpc.health.v1` 健康检查**
- **链路记录**。记录执行节点、重试次数和排队时间，使用内存环形缓冲或可选 SQLite 存储，
  记录模块使用标准库实现
- **Web 管理端**。提供登录、总览、实时请求流、请求调试、配置写回、集群节点管理和组件装卸，
  前端无需构建
- **AI 可调用**。提供结构化输出、分类退出码和随包分发的技能包
- **完整类型标注**。随包提供 `py.typed`

## 📦 安装

```bash
pip install ipclick                     # 只带 curl_cffi，17 个包
pip install "ipclick[niquests]"         # 加 HTTP/2 + HTTP/3
pip install "ipclick[camoufox]"         # 加浏览器渲染（还需下载浏览器本体）
```

可选适配器按用途安装，项目未提供 `[all]`。浏览器引擎还需要单独准备浏览器本体。
引擎选择与安装步骤见 [安装](https://github.com/yuanqimanong/IPClick/wiki/Installation)。

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

默认端口分别为 **gRPC 9528** 与 **Web 9527**。SDK 和其他节点连接 gRPC 端口，
浏览器访问 Web 端口。客户端误连 Web 端口时会在 gRPC 握手阶段失败，错误信息中会附带端口提示。

### 服务端并发与多进程

服务端默认在线程池中处理请求，`[SERVER].max_workers` 控制单个进程的线程数。
需要利用多个 CPU 核时，可配置多个工作进程，通过 SO_REUSEPORT 共享监听端口，
由内核分发连接。客户端仍使用同一个服务地址。

```toml
[SERVER]
processes = 4            # 0 = 按 CPU 核数自动（上限 8）；1 = 单进程（默认）
max_concurrent_rpcs = 0  # 0 = max_workers × 8。决定"排队能排多长"，
                         # 和 max_workers（"同时能干多少活"）是两件事
async_mode = false       # true = grpc.aio 协程模式（实验性，默认关）
```

**多进程模式仅支持 Unix。** Windows 上缺少 `os.fork` 时会告警并降级为单进程。
Web 管理端只在 0 号进程中启动。每个工作进程各自持有适配器和连接池，配置进程数时需计算
这部分内存开销，尤其是启用浏览器渲染的节点。

协程模式控制单个进程内的并发方式，多进程用于利用多个 CPU 核，两项可以组合使用。

基准数据、按 host 限流与容量规划见 [性能与容量](https://github.com/yuanqimanong/IPClick/wiki/Performance)。
使用以下命令初始化配置。

```bash
ipclick init
```

该命令生成 `ipclick.toml`（行为配置）和 `.env`（机密），并在 `.env` 中填入随机 Web 密码。
POSIX 上 `.env` 使用 **600** 权限，Windows 上由目录的 NTFS ACL 控制访问。
已有 `.gitignore` 会补入缺少的 `.env` 规则。新目录需要自行创建 `.gitignore` 并加入该项。
已有配置文件会被保留，需要覆盖时使用 `--force`。

也可以分别导出配置模板。

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

机密也可以写入 `ipclick.toml`，启动时会列出提醒，避免凭据随配置进入 Git、备份或 CI 日志。
确认允许这种配置时，可设 `[SECURITY].allow_secrets_in_config = true` 关闭提醒。
同一项同时出现在配置文件和环境变量中时，**环境变量优先**。

`ipclick config-info` 会逐项显示机密来源（环境变量 / 配置文件 / 未配置），
同时列出当前生效的 TLS、鉴权、限流、浏览器引擎和运行模式。

查看生效配置。

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
        headers={"User-Agent": "MyApp/1.0"},
        params={"key": "value"},
        timeout=30,
    )

    # 检查请求是否成功
    if response.is_success():
        print(f"请求成功，耗时: {response.elapsed_ms}ms")
    else:
        print(f"请求失败: {response.error}")
```

`request()` 及各便捷方法在传输失败、DNS 解析失败时返回 `status_code == -1` 且 `error`
非空的 `DownloadResponse`。参数、准入策略或依赖问题会抛出下表中的异常。

| 异常 | 什么时候 |
|---|---|
| `ValidationError` | 参数不合法（URL 为空、方法不支持…），**以及重定向超过 10 跳** |
| `URLNotAllowedError` | 入口 URL **或任何一跳重定向目标**被 `[SECURITY]` 拒绝 |
| `AdapterError` | 适配器/浏览器没准备好 |
| `HostLimitTimeout` | 等 per-host 配额超时 |

代理、流式下载、断点续传、批量、异步和指定适配器的用法见
[SDK 用法](https://github.com/yuanqimanong/IPClick/wiki/SDK-Usage)。

## 🖥️ Web 管理端

```bash
ipclick run --web        # 或 -w
```

启动时会输出访问地址和用户名。未配置密码时会随机生成并打印，重启后失效。
固定凭据可通过环境变量、`.env` 或 `[WEB].username` / `password` 配置，已配置的密码不会回显。
推荐将凭据放在环境变量或 `.env` 中。

```bash
IPCLICK_WEB_USER=ops IPCLICK_WEB_PASSWORD=... ipclick run -w
```

| 页面 | 干什么 |
|---|---|
| **总览** `/` | 吞吐、成功率、状态码分布、各适配器耗时与流量、集群拓扑、最近请求 |
| **请求流** `/trace` | 实时看请求打进来，按状态 / 适配器 / URL 过滤，看站点排行与按天趋势 |
| **试一试** `/test` | 填网址或直接粘一条 curl，就地发一次请求看链路与源码；可点名打到某个节点 |
| **组件** `/components` | 五个可选 extras 的两级安装状态，可就地安装 / 卸载 / 下载浏览器本体 |
| **配置** `/config` | 75 项配置分 10 组（可折叠），写回 `ipclick.toml`（保留注释、留 `.bak`）；隧道代理整串粘贴自动拆分（地址进 toml、凭据进 `.env`）；集群节点增删 |
| **AI 接入** `/skill` | 技能包全文、安装命令与 `SKILL.md` 下载 |

「试一试」通过本进程的 `TaskService` 执行请求，应用相同的 SSRF 准入、限流和转发规则，
执行记录也会出现在请求流中。

涉及安全边界的设置需在部署时配置，界面会限制编辑这些项目。
包括 `[SECURITY]` 全部、Web 登录凭据、集群共享密钥与节点 token、
`[BROWSER].allow_scripts`、`[BROWSER].executable_path`、`[CLUSTER].allow_remote_install`。
后三项分别影响页内脚本执行、本机程序启动和远程装包权限。

> ⚠️ 界面本身是**明文 HTTP**，默认只监听 `127.0.0.1`。要远程访问请用 SSH 隧道，或放在做了
> TLS 终止的反向代理之后。局域网自用可以 `--web-lan`（等价于 `--web-host 0.0.0.0`），但开
> 之前先把 `[SECURITY].auth_token` 配上——同网段的人绕过网页直连 gRPC 端口是更省事的那条路。

页面功能、配置写回机制、CSP 和会话设计见
[Web 管理端](https://github.com/yuanqimanong/IPClick/wiki/Web-Console)。

## 🤖 命令行（给人，也给 AI）

`init` / `run` / `health` / `config-info` 用于部署和诊断，仅提供可读文本输出。
供脚本和 AI 代理调用的命令支持 `-J/--json`，常用入口如下。

```bash
ipclick status  --json              # 服务端在不在、这台机器能用哪些适配器
ipclick fetch   <URL> --json        # 发一次请求
ipclick trace   list -n 20 --json   # 查链路记录
ipclick node    probe --json        # 探集群节点：连得上吗、令牌配对吗
ipclick component list --json       # 五个可选组件的安装状态
ipclick config  show --json         # 生效配置（机密脱敏）
```

启用 `--json` 后，**stdout 仅输出一个 JSON 文档**，日志和进度写入 stderr，可使用
`ipclick ... --json | jq` 处理结果。文档包含 `ok` 和 `exit_code`，其中 `exit_code` 与进程退出码一致。

| 退出码 | 含义 | 往哪儿查 |
|---|---|---|
| 0 | 成功 | — |
| 1 | 拿到响应但不理想（HTTP ≥ 400、探测不通、装包失败） | 目标本身 |
| 2 | 命令行参数写错了 | `--help` |
| 3 | 连不上 IPClick **服务端**（请求根本没发出去） | 进程 / 地址端口 / 防火墙 |
| 4 | 鉴权失败 | 令牌（`IPCLICK_AUTH_TOKEN`） |
| 5 | 参数被服务端拒绝，或本地配置不合法 | 调用参数或 `ipclick.toml` |

`status` 为 `-1` **或 `null`** 时表示未收到 HTTP 响应。判断执行结果时先读取
`exit_code` / `ok`，再用 `reached_server` 区分请求是否到达 IPClick 服务端。

`--json` 输出的 `body` 默认最多保留 **65536 个字符**，超出时标记 `body_truncated: true`。
`--max-body 0` 可输出完整内容，`-o <file>` 写文件也保留完整响应体。图片、压缩流等非 UTF-8
响应使用 `body_encoding` 为 `base64` 的表示，超过上限时返回空串，避免输出无法解码的片段。

随 wheel 分发的技能包介绍适用场景、输出格式和调用注意事项。可用以下命令安装到 AI 代理目录。

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

镜像基于 **python:3.14-slim**，采用多阶段构建并以非 root 用户运行。
默认包含 patchright 和 Chromium 本体，使用 `-a patchright` 即可渲染 JS 页面。

纯 HTTP 场景可构建精简版，减少约 1.5 GB 的浏览器文件。

```bash
docker build -f docker/Dockerfile --build-arg ENGINE=none -t ipclick:slim .
```

`ENGINE` 可选 `patchright`（默认）/ `camoufox` / `linux`（两个都装）/ `none`。
详细说明见 [`docker/构建.md`](https://github.com/yuanqimanong/IPClick/blob/master/docker/%E6%9E%84%E5%BB%BA.md)。

## 🚧 已知限制

部署前需了解以下功能边界。

- **服务发现只支持 `static` 与 `dns`**。etcd / Consul 的原生 API 没接（Consul 可以走它的
  DNS 接口）。
- **分块下载与临时存储**未实现。目前可使用流式下载处理大文件。
- **集群流式只有建流这一步会故障转移**。流建立之后中途断掉不会自动重连——那需要 Range 请求
  才能不重复数据。批量请求整批发给同一个节点，不跨节点拆分。
- **没有文件上传字段**。要发 multipart 就自己拼好请求体，用 `data=<bytes>` 加上
  `Content-Type: multipart/form-data; boundary=...` 发出去。`files=` 不在协议里，进程内直接
  传给适配器时会抛 `ValidationError`。
- **查询参数原样传递。** `params={"start": "2024-01-01"}` 发出去就是 `start=2024-01-01`。
- **两次请求之间不保持 Cookie 会话。** `curl_cffi` / `niquests` 会复用按连接属性缓存的
  Session，并在每次请求前清空 cookie jar，避免不同调用方共用 Cookie。
  单次请求内部（含重定向链）的 Cookie 由底层库处理。需要携带会话状态时，请在请求中显式传入 `cookies=`。

## 🛠️ 开发

```bash
uv sync --all-groups            # 安装含开发依赖
uv run pytest                   # 运行测试
uv run ruff check src/ tests/   # 代码检查
uv run ruff format src/ tests/  # 代码格式化
uv run basedpyright src/ tests/ # 类型检查
```

修改 `src/ipclick/dto/proto/task.proto` 后需重新生成代码。

```bash
uv run python src/ipclick/dto/proto/generate.py
```

依赖按用途分组（`test` / `lint` / `proto` / `release`，`dev` 是聚合组），CI 按任务安装依赖。
`[camoufox]` 与 `[playwright]`、`[linux]` 与 `[playwright]` 在 `[tool.uv].conflicts` 中分别声明为互斥。
当前 Camoufox 0.5.9 要求 `playwright<1.63`，锁文件为 Camoufox 和独立 Playwright extra 分别解析依赖。
安装可选适配器时，请逐项列出 `--extra`。
同时选择互斥组合或使用 `--all-extras` 会导致解析失败。

项目结构、门禁清单、提交规范与发布流程见 [开发](https://github.com/yuanqimanong/IPClick/wiki/Development)。

## 🤝 贡献

欢迎贡献代码、报告问题或提出建议！请通过 [GitHub Issues](https://github.com/yuanqimanong/IPClick/issues)
或 [Pull Requests](https://github.com/yuanqimanong/IPClick/pulls) 参与项目开发。

## 📄 许可证

本项目采用 [MIT License](https://github.com/yuanqimanong/IPClick/blob/master/LICENSE) 开源许可证。

Copyright (c) 2025 元气码农少女酱
