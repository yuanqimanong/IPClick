# IPClick

![IPClick Logo](https://raw.githubusercontent.com/yuanqimanong/IPClick/master/docs/logo.png)

> IPClick 名字灵感来源于动画《Link Click》（时光代理人）。正如时光代理人穿梭于不同的时空执行任务，IPClick 帮助您将 HTTP 请求分发到不同的节点高效执行。

## 📖 简介

IPClick 是一个轻量级、高性能的分布式 HTTP 请求代理工具，基于 gRPC 协议构建。它提供了统一的请求接口，支持多种 HTTP
客户端适配器，帮助开发者更高效地处理网络请求。

> 📚 **完整文档在 [Wiki](https://github.com/yuanqimanong/IPClick/wiki)** ——
> [安装](https://github.com/yuanqimanong/IPClick/wiki/Installation) ·
> [快速开始](https://github.com/yuanqimanong/IPClick/wiki/Quick-Start) ·
> [配置体系](https://github.com/yuanqimanong/IPClick/wiki/Configuration) ·
> [集群](https://github.com/yuanqimanong/IPClick/wiki/Cluster) ·
> [浏览器渲染](https://github.com/yuanqimanong/IPClick/wiki/Browser-Rendering) ·
> [故障排查](https://github.com/yuanqimanong/IPClick/wiki/Troubleshooting)

## ✨ 特性

- **多适配器支持**：默认 `curl_cffi`，按需加装 `niquests` / 浏览器渲染，并可注册自定义适配器
- **轻量安装**：`pip install ipclick` 只带 curl_cffi，其余全部走 extras
- **浏览器指纹伪装**：基于 `curl_cffi` 实现浏览器指纹模拟，有效绕过反爬检测
- **浏览器渲染**：起真实浏览器执行 JS，四个引擎可选（camoufox / patchright /
  playwright / DrissionPage），默认按平台挑
- **集群与故障转移**：多节点客户端，支持轮询 / 随机 / 加权均衡、健康探测与自动换节点
- **gRPC 通信**：使用 gRPC 协议进行高效的客户端-服务端通信
- **连接复用**：客户端复用 gRPC channel，服务端复用适配器与 HTTP 连接池
- **代理支持**：灵活的代理配置，支持 HTTP/HTTPS 代理
- **自动重试**：适配器内重试（指数退避 + 抖动、按状态码）+ 客户端到服务端这一跳的重试
- **按 host 限流**：服务端按目标域名严格限制并发数与 QPS，避免把单个站点打爆
- **流式下载**：大文件分片传输，服务端与客户端都不需要把整个响应体驻留内存
- **断点续传**：中断后用 HTTP Range 接着下，`If-Range` 保证不会拼接两个版本
- **批量请求**：一次 RPC 处理多个任务，结果按完成顺序流式返回
- **异步客户端**：基于 `grpc.aio` 的 `AsyncDownloader`，与同步版接口对应
- **令牌鉴权**：gRPC 标准 Bearer 令牌，支持环境变量注入与多令牌轮换
- **TLS / mTLS**：链路加密与双向证书认证，客户端、服务端、集群探活全通路覆盖
- **健康检查**：实现 `grpc.health.v1` 标准协议，K8s 探针与服务网格开箱即用
- **链路记录**：每个请求记一条（谁执行的、重试几次、排队多久），内存环形缓冲 + 可选 SQLite，零第三方依赖
- **SSRF 防护**：服务端对目标 URL 做协议白名单与内网/元数据地址拦截
- **Web 管理端**：`ipclick run --web` 起一个带登录的界面——总览看板、实时请求流、
  「试一试」就地发请求看源码、配置改完写回 `ipclick.toml`、集群节点增删
- **命令行工具**：提供便捷的 CLI 工具，支持快速启动服务和查看配置
- **Docker 支持**：多阶段构建、非 root 运行的镜像
- **完整类型标注**：随包提供 `py.typed`，下游可直接享受类型检查

## 📦 安装

### 从 PyPI 安装

```bash
pip install ipclick
```

`pip install ipclick` 是**轻量安装**——只带默认的 curl_cffi 适配器。其余按需加：

```bash
pip install "ipclick[niquests]"     # niquests 适配器（requests 的 drop-in，支持 HTTP/2、HTTP/3）
pip install "ipclick[camoufox]"     # 浏览器渲染：Firefox 反检测（Linux/macOS 默认）
pip install "ipclick[drissionpage]" # 浏览器渲染：CDP 直连（Windows 默认）
pip install "ipclick[patchright]"   # 浏览器渲染：Chromium 反检测
pip install "ipclick[playwright]"   # 浏览器渲染：原版 playwright

# 按平台打包好的组合
pip install "ipclick[win]"          # curl_cffi + DrissionPage
pip install "ipclick[linux]"        # curl_cffi + camoufox + patchright
```

**没有 `[all]`**，这是刻意的：四个浏览器内核全装是 70+ 个包和上 G 的浏览器本体，
而一台机器只会用其中一个。也**没有 `[redis]` / `[metrics]`**——集群不再需要中间件
（见[集群](#集群)），统计改成内置的[链路记录](#链路记录与统计)。

浏览器引擎是**两步**安装 —— `pip install` 只装 Python 包（几 MB），浏览器本体要单独准备：

```bash
python -m camoufox fetch        # camoufox：下载它自己那份 Firefox（约 1 GB，装到 ~/.cache/camoufox）
patchright install chromium     # patchright
playwright install chromium     # playwright
# DrissionPage 用本机已装的 Chrome/Chromium，不用额外下载
```

只做第一步的话，`ipclick config-info` 与 Web 端会明确显示 **「包已装，浏览器本体未就绪」**，
请求会直接报 `FAILED_PRECONDITION` 并告诉你该跑哪条命令。

> **刻意不让它在请求里自动下载。** camoufox 的 API 默认行为是"缺本体就当场下"
> （`camoufox_path(download_if_missing=True)`），而那一刻已经在 gRPC 的请求处理线程上：
> 第一个请求会卡着下 1 GB 然后超时，超时返回后下载还在后台跑，并发的多个首请求
> 还可能各自触发一次。所以本项目自己解析路径并显式传给它，缺了就报错。
> **连"查一下装没装"也不能走它的接口** —— `launch_path()` 内部同样会触发下载，
> 光渲染一次 Web 端总览页就够了。

playwright / patchright 也可以复用系统已有的浏览器（省掉约 150MB 下载）：

```toml
[BROWSER]
executable_path = "/usr/bin/chromium"
```

### 从源码安装

本项目使用 [uv](https://docs.astral.sh/uv/) 管理依赖：

```bash
git clone https://github.com/yuanqimanong/IPClick.git
cd IPClick
uv sync
```

也可以直接用 pip 以可编辑方式安装：

```bash
pip install -e .
```

## 🔧 系统要求

- Python >= 3.11
- 核心依赖（`pip install ipclick` 会装这些，共 17 个包）：
    - curl-cffi >= 0.16.0（默认适配器）
    - grpcio >= 1.83.0 / grpcio-health-checking / protobuf >= 6.33.2
    - click >= 8.4.2
    - fake-useragent >= 2.2.0
    - loguru >= 0.7.3
    - python-box >= 7.4.1
    - uuid-utils >= 0.17.0
- niquests、四个浏览器引擎全部走 extras，不装就不引入

## 🚀 快速开始

### 启动服务端

```bash
# 使用默认配置启动
ipclick run

# 指定端口和地址
ipclick run --host 0.0.0.0 --port 9527

# 使用自定义配置文件（TOML 格式）
ipclick run --config /path/to/ipclick.toml

# 显示详细日志
ipclick run --verbose
```

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

### Web 管理端

```bash
ipclick run --web        # 或 -w
```

启动时会把访问地址和登录信息打到控制台：

```
==============================================================
  IPClick Web 管理端: http://127.0.0.1:9530/
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

#### 五个页面

| 页面 | 干什么 |
|---|---|
| **总览** `/` | 吞吐、成功率、在途与峰值、各适配器耗时与流量、状态码分布、引擎安装状态、集群拓扑、最近 12 条请求 |
| **请求流** `/trace` | **实时**（3 秒刷新）看请求打进来。按状态 / 适配器 / URL 过滤，看目标站点排行与按天趋势 |
| **试一试** `/test` | 填一个网址就地发一次请求，看链路信息与返回的**源码** |
| **配置** `/config` | 白名单内的行为配置可改，**写回 `ipclick.toml`** |
| **节点** `/nodes` | 集群节点的增删改，同样写回 `ipclick.toml` |

「试一试」走的是本进程 `TaskService` 的**同一条**代码路径——SSRF 准入、限流、
以及开了转发时的分发全都照常生效，请求也会像真实请求一样出现在请求流里。
另写一条只在页面上成立的路径毫无意义：那验证的就不是线上行为了。

#### 能改什么、不能改什么

- **能改**（写回 toml，保留注释与格式，改动前留 `.bak`）：超时、重试、按 host
  限流、日志级别、浏览器引擎与页面上限、链路记录、集群策略与节点列表、压缩策略。
- **不能改**：`[SECURITY]` 全部（令牌、TLS、SSRF 三个开关）、Web 自己的登录凭据、
  集群共享密钥与各节点 token、`[BROWSER].allow_scripts`。这些只展示，要改请编辑
  文件 / `.env` 后重启。
- **不装东西**：引擎的安装状态只**展示**，附上安装命令让人自己去那台机器上跑。
  让网页能执行安装命令等于给它一个任意命令执行的入口。

这条线是刻意划的：这个服务能代任意 URL 发请求，一个能从网页关掉内网拦截、改掉
令牌的管理端，等于给自己装了个跳板。而调超时、加节点这类操作改错了也只是性能
或可用性问题，可逆、可见。

写回是**定点文本替换**而不是"读成 dict 再整体 dump"：那份配置里几乎每一项都带着
解释（为什么默认是这个值、配错了什么症状），整体 dump 会把它们全抹掉。所以只换
被改动那一行等号右边的值，行尾注释都保留。写入用临时文件 + `os.replace`，
断电不会留下半个配置文件。

页面里**一行 JavaScript 都没有**，自动刷新用 `<meta refresh>`，CSP 收到
`default-src 'none'`。

安全措施：会话 cookie 带 `HttpOnly` + `SameSite=Strict`，所有写操作校验 CSRF
token，登录失败按来源 IP 限速（5 次后锁定 5 分钟，锁定期间正确密码也不放行），
密码用常量时间比对，`X-Frame-Options: DENY` + 严格 CSP。

> ⚠️ 界面本身是**明文 HTTP**。默认只监听 `127.0.0.1`；要远程访问请用 SSH 隧道，
> 或放在做了 TLS 终止的反向代理之后。直接把 `[WEB].host` 改成 `0.0.0.0` 会让
> 登录密码在网络上裸奔（启动时会打告警）。

### 配置优先级

从高到低：

1. 命令行参数 / 构造函数参数
2. 环境变量
3. 当前目录的 `.env`（**不覆盖**已存在的环境变量）
4. `ipclick.toml` / `.ipclick.toml`，或 `--config` 指定的文件
5. `~/.ipclick/config.toml`
6. 包内默认配置

`.env` 排在真实环境变量之后是有意的：容器编排、CI、systemd 注入的变量必须能压过
仓库里那个用于本地开发的 `.env`，否则部署环境会被开发默认值悄悄改掉。

**部署参数**（非机密）也可以用环境变量覆盖。它们**刻意不进 `.env` 模板**——那是
放密钥的文件，这些是给容器编排 / K8s / systemd 注入的：

| 变量 | 覆盖 |
|---|---|
| `IPCLICK_HOST` / `IPCLICK_PORT` | `[SERVER].host` / `port` |
| `IPCLICK_MAX_WORKERS` | `[SERVER].max_workers` |
| `IPCLICK_MODE` | `[GENERAL].mode` |
| `IPCLICK_LOG_LEVEL` | `[LOG].level` |
| `IPCLICK_CLUSTER_SELF_ID` | `[CLUSTER].self_id` —— 多台机器共用一份配置时靠它区分身份 |

`.env` 支持 `KEY=VALUE`、`export KEY=VALUE`、`#` 注释、单双引号（双引号内可转义）。
不支持多行值和 `${VAR}` 插值——需要那些请直接用环境变量。

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

### 使用代理

```python
from ipclick import Downloader, ProxyConfig, HttpMethod

downloader = Downloader()

# 方式一：使用代理配置对象
proxy = ProxyConfig(
    scheme="http",
    host="proxy.example.com",
    port=8080,
    auth_key="username",
    auth_password="password",
)

response = downloader.request(
    method=HttpMethod.GET,
    url="https://httpbin.org/ip",
    proxy=proxy,
)

# 方式二：使用代理 URL 字符串
response = downloader.request(
    method=HttpMethod.GET,
    url="https://httpbin.org/ip",
    proxy="http://user:pass@proxy.example.com:8080",
)

# 方式三：使用配置文件 [PROXY] 中的代理（设置 proxy=True）
response = downloader.request(
    method=HttpMethod.GET,
    url="https://httpbin.org/ip",
    proxy=True,
)
```

> 适配器默认**不读取**环境变量里的 `HTTP_PROXY` / `ALL_PROXY`。代理必须由调用方显式指定，
> 以免请求意外走到服务端所在机器的环境代理出口。

### 流式下载（大文件）

`get()` 会把整个响应体读进内存；大文件请用 `stream()`，响应体分片到达，
状态码和响应头在收到第一个字节前就可用：

```python
with downloader.stream("https://example.com/big.zip") as resp:
    print(resp.status_code, resp.content_length)
    if not resp.is_success():
        raise SystemExit(resp.error)

    with open("big.zip", "wb") as f:
        for chunk in resp:          # 每片默认 64KB
            f.write(chunk)

    print(f"共 {resp.total_bytes} 字节，耗时 {resp.elapsed_ms}ms")
```

提前退出 `with` 会 cancel 掉这条 gRPC 流，服务端也就不再继续下载。

> 流式路径**不做重试** —— 重试要么得缓存已发出的分片、要么让调用方看到重复数据，
> 两者都不可接受。流式请求失败就是失败，由调用方决定是否重来。

### 断点续传

`stream()` 中途断了要从头再来。大文件抓取里这最疼——下到 90% 断线，前面全白费。
`download_to_file()` 在流式通路上做 Range 续传：

```python
from ipclick import Downloader
from ipclick.resume import download_to_file

with Downloader() as d:
    result = download_to_file(
        d, "https://example.com/big.zip", "big.zip",
        max_attempts=5,
        chunk_callback=lambda done, total: print(f"{done}/{total}"),
    )
    print(result.total_bytes, f"（发了 {result.attempts} 次请求）")
```

不落盘的版本是 `iter_resumable()`，直接产出分片。

**它为什么不只是"断了带 Range 重来"**：如果目标文件在两次请求之间变了，那样会把
新版本的后半段接到旧版本的前半段上，拼出一个既不是旧版也不是新版、而且**校验不
出来**的文件——比下载失败严重得多。所以每次续传都带 `If-Range`（ETag，没有就用
Last-Modified）：资源没变服务端回 206 接着写，变了回 200 就丢掉重来。

服务端不支持 Range（没有 `Accept-Ranges: bytes`）时自动退化成整体重下，不会悄悄
产出损坏文件。`iter_resumable()` 遇到资源变化会直接抛错——分片已经交给调用方了
收不回来，继续下去只会让它拿到两个版本的混合体。

### 批量请求

一次 RPC 处理多个任务，省掉逐个调用的往返开销：

```python
from ipclick import DownloadTask

tasks = [DownloadTask(uuid=url, url=url) for url in urls]
for resp in downloader.batch(tasks):
    print(resp.request_uuid, resp.status_code)
```

结果按**完成顺序**产出（不是提交顺序），所以要靠 `request_uuid` 对应回请求。
单个任务失败不影响其他任务，失败信息在各自响应的 `error` 里。
并发度受服务端 `[SERVER].max_workers` 约束。

### 异步客户端

```python
from ipclick.aio import AsyncDownloader

async with AsyncDownloader() as d:
    resp = await d.get("https://example.com")

    stream = await d.stream("https://example.com/big.zip")
    async with stream:
        async for chunk in stream:
            ...

    async for resp in d.batch(tasks):
        ...
```

参数含义、默认值、异常类型与同步版完全一致（两者共用同一套配置与任务组装逻辑）。

### 单机还是集群，由配置决定

`create_client()` 按 `[GENERAL].mode` 返回 `Downloader` 或 `ClusterDownloader`，
两者的请求接口一致，调用方通常不用关心：

```python
from ipclick import create_client

with create_client() as d:          # mode = standalone / cluster / auto
    resp = d.get("https://example.com")
```

`mode = "cluster"` 却没配任何节点会**直接报错**，不会静默退回单机——那会让你
以为集群生效了，实际所有流量都打在一个节点上、也没有故障转移。

### 客户端重试

适配器内部的重试解决的是"目标站点抖了"；`[CLIENT]` 这组解决的是"我们自己的
服务端抖了"：

```toml
[CLIENT]
rpc_max_retries = 2
rpc_retry_backoff = 0.5
```

**只有 `UNAVAILABLE` 会重试**——那意味着连接压根没建起来，请求没到过服务端，
重发是安全的。`DEADLINE_EXCEEDED` 刻意不重试：请求可能已经在服务端执行了，
只是回复没赶上，这时重发一个 POST 就是重复下单。

### 使用全局下载器

```python
from ipclick import downloader

# 使用默认的全局下载器实例（首次使用时才真正建立连接）
response = downloader.get("https://httpbin.org/ip")
print(response.text)
```

### 指定适配器

```python
from ipclick import IPClickAdapter, downloader

response = downloader.get("https://httpbin.org/get", adapter=IPClickAdapter.NIQUESTS)
```

| 适配器 | 反检测 | HTTP/2 · 3 | JS 渲染 | 安装 |
|---|---|---|---|---|
| `curl_cffi`（默认） | TLS 指纹伪装 | HTTP/2 | ❌ | 内置 |
| `niquests` | ❌ | **HTTP/3** | ❌ | `ipclick[niquests]` |
| `browser` | 由服务端引擎决定 | — | ✅ | 见下 |
| `camoufox` | Firefox + 完整指纹伪装 | — | ✅ | `ipclick[camoufox]` |
| `patchright` | Chromium，Playwright 反检测分支 | — | ✅ | `ipclick[patchright]` |
| `playwright` | ❌（原版，最稳） | — | ✅ | `ipclick[playwright]` |
| `DrissionPage` | Chromium，CDP 直连 | — | ✅ | `ipclick[drissionpage]` |

> `requests` 适配器在 0.2.3 里有过，之后被 `niquests` 取代——后者 API 完全一致
> 但支持 HTTP/2 和 HTTP/3。请求 `requests` 会报错并给出迁移提示。

### 浏览器渲染

页面内容全靠 JS 生成时，HTTP 适配器拿到的 HTML 里什么都没有。浏览器适配器会起
一个真实浏览器把页面跑完，返回渲染后的 DOM：

```python
from ipclick import Downloader, IPClickAdapter

with Downloader() as d:
    resp = d.get(
        "https://example.com/spa",
        # BROWSER = "渲染就行，引擎由服务端定"
        adapter=IPClickAdapter.BROWSER,
        # 声明式的等待与渲染选项
        automation_config='{"wait_for_selector": "#content", "scroll_to_bottom": true}',
    )
    print(resp.text)          # 渲染后的 DOM
```

#### 引擎选择

四个引擎。前三个都走 Playwright API，共用同一套渲染代码；DrissionPage 走 CDP
直连，是另一套实现，但对外契约一致。

| 引擎 | 内核 | 特点 | 额外准备 |
|---|---|---|---|
| `camoufox` | Firefox | 反检测最彻底，自带完整指纹伪装 | `python -m camoufox fetch` |
| `patchright` | Chromium | Playwright 反检测分支，API 全兼容 | `patchright install chromium` |
| `playwright` | 三种内核 | 原版，最稳、行为最可预期 | `playwright install chromium` |
| `DrissionPage` | Chromium | CDP 直连本机 Chrome，不额外下浏览器 | 本机已装 Chrome/Chromium |

`[BROWSER].engine` 决定 `adapter=BROWSER` 用哪个，默认 `auto` **按平台选**：

- **Windows** → `DrissionPage`（基本都装了 Chrome，最省事）
- **Linux / macOS** → `camoufox`（自带 Firefox，无头服务器上更好伺候）

```toml
[BROWSER]
engine = "auto"     # 或 camoufox / patchright / playwright / drissionpage
```

客户端也可以直接点名某个引擎（`adapter=IPClickAdapter.CAMOUFOX` 等），这时
`[BROWSER].engine` 不生效——点名就是点名。

各引擎的差异，挑要紧的说：

- **camoufox 自己管指纹**。它会生成一整套自洽的 UA / 屏幕 / 字体 / WebGL 指纹，
  所以 `[BROWSER].user_agent` 和 `viewport` 对它无效（强行覆盖只会和它给的指纹
  自相矛盾，反而更容易被认出来）。要调就用它认识的 `locale` / `humanize` / `geoip`。
- **camoufox 更吃内存**。Firefox 加一整套扩展，单个 context 的开销明显高于
  Chromium 系。内存 ≤4GB 的机器建议把 `max_pages` 设成 1~2，否则会开始换页，
  请求从几秒变成几分钟。
- **DrissionPage 不支持按请求指定代理**。它的代理是浏览器进程级的，启动后改不了，
  所以请求里带 `proxy=` 会直接抛 `ValidationError` 而不是被默默忽略——对一个代理
  服务来说，从错误的出口 IP 发出去比报错严重得多。要用代理请配
  `[BROWSER.proxy].gateway`，或换 camoufox / patchright / playwright。
- **DrissionPage 的隔离弱一些**。它没有 BrowserContext，同一浏览器里所有 tab 共享
  profile。这里用 `--incognito` 启动、每请求一个用完即关的 tab、并在关闭前清一次
  cookie，但仍不如 Playwright 的 context 干净。

`automation_config` 支持的键：

| 键 | 说明 |
|---|---|
| `wait_until` | `load` / `domcontentloaded` / `networkidle` / `commit` |
| `wait_for_selector` | 等某个元素出现 |
| `wait_for_timeout` | 额外固定等待（毫秒） |
| `scroll_to_bottom` | 滚到底触发懒加载（高度不再变化即停，最多 20 轮） |
| `screenshot` | 返回整页 PNG（此时 `content` 是图片字节，`text` 为空） |
| `block_resources` | 覆盖默认拦截的资源类型 |

浏览器本身带来的限制（不是没写，是做不到）：

- **只支持 GET**：浏览器导航就是 GET。带请求体或用其他方法会抛 `ValidationError`。
- **不能禁用重定向**：`allow_redirects=False` 会抛 `ValidationError`，而不是被静默忽略。
- **流式无意义**：渲染必须等页面跑完才有结果。

> ⚠️ `automation_script`（在页面里执行任意 JS）默认**关闭**。页面内的 JS 会自己
> 发请求，`[SECURITY]` 那套 URL 策略对它完全不起作用——放开它等于把 SSRF 防线
> 让开一整条。确认调用方全部可信后，再设 `[BROWSER].allow_scripts = true`。
> 脚本返回值通过响应头 `x-ipclick-script-result` 带回（JSON 编码）。
>
> 另外，渲染本身就会加载页面引用的子资源，同样不经过 URL 策略。介意的话把
> `block_resources` 配严一些，并打开 `[SECURITY].block_private_networks`。

### TLS / mTLS

gRPC 链路默认是**明文**的。令牌鉴权解决的是"谁能用"，但令牌本身在不受信任的
网络上会被同网段原样嗅探到——鉴权、限流、SSRF 防护全都建在这条明文通道上。
公网或跨机房部署务必打开 TLS。

```toml
[SECURITY.tls]
enabled = true
cert_file = "/etc/ipclick/server.crt"   # 服务端证书
key_file  = "/etc/ipclick/server.key"
# 下面两项一起构成 mTLS：服务端反过来验证客户端证书
ca_file = "/etc/ipclick/ca.crt"
require_client_cert = true
```

客户端用同一个配置节，各取所需：

```toml
[SECURITY.tls]
enabled = true
ca_file = "/etc/ipclick/ca.crt"          # 验证服务端；留空则用系统信任库
cert_file = "/etc/ipclick/client.crt"    # 服务端要求 mTLS 时才需要
key_file  = "/etc/ipclick/client.key"
```

也可以在代码里直接给：

```python
from ipclick import Downloader
from ipclick.tls import TLSSettings

tls = TLSSettings(enabled=True, ca_file="ca.crt", cert_file="client.crt", key_file="client.key")
with Downloader(tls=tls) as d:
    resp = d.get("https://example.com")
```

几点值得说明：

- **TLS 和令牌是两回事，互不替代**。证书回答"这条连接可不可信"，令牌回答"这个
  调用方是谁"。两者可以同时开。
- **`require_client_cert = true` 必须同时配 `ca_file`**，否则任何自签名证书都能
  通过，mTLS 形同虚设。这种组合会在启动时直接报错，不会静默降级。
- **集群探活也走 TLS**。服务端开了 TLS 而探活还是明文的话，集群会把健康节点
  全判成挂了。
- **证书签给域名、却用 IP 连**时，用 `server_name_override` 对上名字。
  它会跳过主机名匹配这道校验，只应在自签名证书的内网环境里用。
- 服务端监听非回环地址却没开 TLS 时，启动日志会打显著告警。

自签名证书（内网自用）可以这样生成：

```bash
openssl req -x509 -newkey rsa:4096 -nodes -keyout ca.key -out ca.crt -days 3650 -subj "/CN=ipclick-ca"
openssl req -newkey rsa:4096 -nodes -keyout server.key -out server.csr -subj "/CN=ipclick"
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out server.crt -days 825 \
  -extfile <(printf "subjectAltName=DNS:ipclick,IP:10.0.0.5")
```

> gRPC 校验的是证书里的 **SAN**，不是 CN——只写 `-subj "/CN=..."` 而没有
> `subjectAltName` 的证书，在现代 TLS 栈里一律会被拒。

### 按 host 限流

服务端会代替调用方向外发请求。不加约束的话，一个批量任务就能对同一个站点瞬间
打出几十上百个并发连接——对方大概率先限速再封 IP。两道闸门，都是**按 host 独立**
计数，默认都关闭：

```toml
[DOWNLOADER.concurrency]
# 单个 host 同时最多几个在途请求（0 = 不限制）。超出的在服务端排队。
per_host_max_concurrent = 4
# 等不到额度的上限（秒），超时返回 RESOURCE_EXHAUSTED
per_host_wait_timeout = 30

[DOWNLOADER.rate_limit]
# 单个 host 每秒最多发起几个请求（0 = 不限制）。可以是小数：0.5 = 两秒一个。
per_host_qps = 2
# 突发额度（令牌桶容量）。0 = 取 ceil(per_host_qps)
per_host_burst = 0
```

并发用信号量（硬保证），速率用令牌桶（允许突发）。取额度的顺序是**先并发槽、
后令牌**：并发槽是要保证的上限，先拿住；令牌紧挨着真正的 HTTP 请求再取，这样
"每秒 N 个"限的是真实发出去的请求，而不是进入排队的请求。

限流对 `Send` / `SendStream` / `SendBatch` 都生效。流式请求的额度**持有到整条流
结束**——只在建流时占额度等于没限住。

**集群里的限额**：额度在**收到任务的那个节点**上计算。用服务端转发模式时所有
任务都从入口节点进来，那一台算出来的就是全局额度，因此**不需要 Redis 之类的
中间件**（0.3 起已移除 `backend = "redis"`）。用客户端分发模式时每个节点各算
各的，N 个节点就是 N 倍实际并发——需要全局限额就改用服务端转发。

> ⚠️ **这会占用 worker 线程。** 服务端是一请求一线程，排队等额度会占着那个
> 线程。`per_host_max_concurrent` 设得很小、同时又有大量请求打向同一个 host 时，
> 线程池会被排队的请求占满，其他 host 的请求也跟着饿死。所以等待有硬性上限
> （`per_host_wait_timeout`），并且 `[SERVER].max_workers` 要留够余量。
> 真要做到"排队不占线程"，得把服务端改成 `grpc.aio`，那是另一件事。

超时会抛 `HostLimitTimeout`，而不是返回 `status_code == -1`：这是本机的限流策略
生效了，不是网络故障，返回 -1 会让人去排查目标站点。

### 集群

有**两种**形态，用 `[CLUSTER].forward` 选。两种都保留，因为它们适合的场景不同。

#### 形态一：客户端分发（默认，`forward = "off"`）

调用方自己持有全部节点地址，直连每一台。少一跳、不占入口带宽，前提是调用方能连
到所有节点。

```python
from ipclick.cluster import ClusterDownloader

with ClusterDownloader() as d:          # 节点取自 [CLUSTER].nodes
    resp = d.get("https://example.com")
    print(d.snapshot())                 # 各节点健康状态
```

#### 形态二：服务端转发（`forward = "on"`）

调用方只需要知道**一个**地址。入口节点按策略挑节点：挑到自己就本地干，挑到别人
就把请求原样转过去，拿到结果再回给调用方。适合调用方在集群外，或者不想让调用方
感知拓扑。**不需要 Redis 之类的中间件**——任务从哪进来，分发和限流就在那一台算。

```toml
[CLUSTER]
forward = "on"
self_id = "node-a"              # 留空则按 [SERVER] 的监听端口 + 本机地址自动识别
load_balancer = "round_robin"   # round_robin / random / weight
max_failover = 2                # 转发失败最多换几个节点
forward_timeout = 0             # 0 = 按任务超时 ×(重试次数+1) + 15s 余量推算

nodes = [
    { id = "node-a", address = "10.0.0.1:9527" },   # 本机也要列进来才会分到活
    { id = "node-b", address = "10.0.0.2:9527" },
    { id = "node-c", address = "10.0.0.3:9527" },
]
```

五台机器可以用**完全相同**的这份 `ipclick.toml` 和**完全相同**的 `.env`，只靠
一个环境变量区分身份：

```bash
IPCLICK_CLUSTER_SELF_ID=node-b ipclick run
```

四条值得知道的性质：

1. **只转一跳。** 转发时带 `ipclick-forwarded` metadata，收到带这个标记的请求
   一律本地执行，所以环路在协议层面就不可能出现。
2. **任意节点都能当入口。** 配置对等，谁被访问谁就是入口。平时只把流量打给 A，
   A 挂了直接把客户端指向 B 即可，不用改任何配置。
3. **入口自己也干活**（只要它在 `nodes` 里）。子节点全挂时入口会自己兜底执行，
   而不是让请求失败。
4. **流式下载不转发**，永远由收到请求的节点自己执行——把每个分片再经入口中转一次
   会让入口带宽翻倍。要让子节点出流量就用客户端分发模式直连它。

批量（`SendBatch`）是转发模式最划算的场景：一次调用会被摊到多台机器上。

#### 节点间鉴权：一个共享密钥，派生出各不相同的令牌

最直觉的做法是给每台机器手写一个令牌、再在每台机器的节点列表里把其他机器的令牌
抄一遍——5 台机器就是 20 份抄写，加一台要改 6 个文件，改漏一处的症状是运行时
`UNAUTHENTICATED`，很难定位。

所以改成派生：

```
node_token = base64url( HMAC-SHA256(cluster_secret, "ipclick-node:" + node_id) )
```

所有机器的 `.env` 里放**同一个** `IPCLICK_CLUSTER_SECRET`：

```
IPCLICK_CLUSTER_SECRET=（用 openssl rand -base64 32 生成，复制到每台机器）
```

* 每个节点用自己的 `self_id` 算出自己该接受哪个令牌。
* 转发方用目标的 `node_id` 算出该带哪个令牌。两边算的必然一致。
* 令牌**各不相同**——拿到子节点 B 的令牌不等于能调 C，也不等于拿到共享密钥
  （HMAC 单向）。这是"每节点独立令牌"和"全集群一个口令"的差别所在。
* 加一台机器只需要在节点列表里加一行，不需要发放任何新凭据。

需要对接令牌不由这里管理的节点时，在那一项上写 `token = "..."` 覆盖派生值。
没配共享密钥也能跑（内网全互信是合法选择），但启动时会明确警告。

#### 共通部分

```toml
[CLUSTER]
probe_interval = 10             # 健康探测间隔（秒）
failure_threshold = 3           # 连续失败多少次判定摘除
recovery_threshold = 2          # 连续成功多少次判定恢复
```

摘除与恢复都用**连续计数阈值**而不是单次结果：一次网络抖动就摘节点会让流量反复
横跳。探测走 `grpc.health.v1`，也就是 P2 那套标准健康检查。

故障转移只在节点级故障（`UNAVAILABLE` / 超时 / 过载）时换节点。参数错误、鉴权
失败换个节点还是一样的结果，直接把错误还给调用方，不浪费尝试次数。

**节点发现**（可选）：节点列表默认写死在配置里，扩缩容要改配置重启每一个客户端。
换成 DNS 发现就不用了：

```toml
[CLUSTER.discovery]
mode = "dns"
dns_name = "ipclick.default.svc.cluster.local"   # K8s headless Service / Consul DNS
port = 9527
refresh_interval = 30
```

解析出的每条 A/AAAA 记录就是一个节点，后台随探活一起定期重解析。刷新时按地址
复用已有节点的健康状态——直接重建会把连续计数清零，那样"连续 2 次失败才摘除"
永远达不到，熔断和恢复双双失效。DNS 解析失败时沿用上一次的列表，不会把集群摘空。

**只读状态页**（可选）：

```python
from ipclick.cluster import ClusterDownloader
from ipclick.cluster.status_page import StatusPageServer

with ClusterDownloader() as d:
    StatusPageServer(d.snapshot).start(port=8080)   # 默认只监听 127.0.0.1
```

`/` 是人看的 HTML 表格，`/api/nodes` 是机器读的 JSON。**刻意做成只读**：这个
服务已经能代任意 URL 发请求了，再给它一个能改配置的网页等于又开一个高价值攻击
面，而状态页通常跑在比 gRPC 端口设防更少的地方。运维变更走配置文件。

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
│       │   └── status_page.py   # 只读状态页
│       ├── web/                 # Web 管理端（仅标准库，页面里没有 JS）
│       │   ├── server.py        # HTTP 服务、会话、CSRF、路由
│       │   ├── pages.py         # 各页面的数据与操作
│       │   ├── templates.py     # HTML 渲染（每处插值都要过 esc）
│       │   ├── editable.py      # 可编辑配置项白名单
│       │   └── auth.py          # 凭据、会话、登录限速
│       ├── cli/                 # 命令行工具
│       ├── config_loader/       # 配置加载
│       │   ├── loader.py        # 优先级与环境变量覆盖
│       │   ├── dotenv.py        # .env 解析
│       │   └── writer.py        # 写回 toml（定点替换，保留注释）
│       ├── configs/             # 默认配置文件
│       ├── dto/                 # 数据传输对象
│       │   ├── models.py        # 数据模型定义
│       │   ├── response.py      # 统一响应对象
│       │   └── proto/           # Protobuf 定义与生成脚本
│       ├── services/            # gRPC 服务实现
│       └── utils/               # 工具模块（日志、配置、URL 安全校验等）
├── docker/                      # Docker 相关文件
├── examples/                    # 示例代码
├── tests/                       # 测试代码
├── pyproject.toml               # 项目配置
└── README.md
```

## ⚙️ 配置说明

配置文件为 TOML 格式，加载顺序（后者覆盖前者）：

1. 包内默认配置 `src/ipclick/configs/default_config.toml`
2. 用户目录配置 `~/.ipclick/config.toml`
3. `--config` 指定的文件，或当前目录下的 `ipclick.toml` / `.ipclick.toml`
4. 环境变量 `IPCLICK_HOST`、`IPCLICK_PORT`（优先级最高）

### 服务端配置

| 配置项                  | 说明                | 默认值     |
|----------------------|-------------------|---------|
| `SERVER.host`        | 服务绑定地址            | `[::]`  |
| `SERVER.port`        | 服务端口              | `9527`  |
| `SERVER.max_workers` | 最大工作线程数（每请求占用一个）  | `100`   |

### 鉴权

服务端默认**不鉴权** —— 任何能连到端口的调用方都能使用本服务，启动时会打一条告警。
生产部署请务必配置令牌。

推荐用环境变量提供，不要把密钥写进配置文件：

```bash
# 服务端
IPCLICK_AUTH_TOKEN='用 openssl rand -hex 32 生成的令牌' ipclick run
```

```python
# 客户端：三种方式，优先级从高到低
Downloader(token="...")                 # 1. 显式传参
# 2. 环境变量 IPCLICK_AUTH_TOKEN
# 3. 配置文件 [SECURITY].auth_token
```

轮换令牌时可以配置多个，新旧并存，不必停机：

```toml
[SECURITY]
auth_token = ["新令牌", "旧令牌"]
```

令牌通过 gRPC 标准的 `authorization: Bearer <token>` metadata 头传输，
任何语言的 gRPC 客户端都能对接。校验使用常量时间比较；令牌不会出现在任何日志里。

鉴权失败抛 `AuthenticationError`（**不是** `TransportError`）——
令牌错了重试多少次都没用，属于配置问题，不该被当成一次网络失败吞掉。

> 鉴权解决的是"**谁**能用"，下面的 SSRF 防护解决的是"能打到**哪儿**"。
> 两者互相不能替代，公网部署两个都要开。

### 健康检查

服务端实现了 gRPC 标准的 `grpc.health.v1` 协议，Kubernetes 探针、
`grpc_health_probe`、服务网格都能直接对接。**该接口免鉴权**。

```bash
ipclick health                    # 查总体状态
ipclick health --port 9527
ipclick health --service task.TaskService
```

健康时退出码 `0`，否则 `1` —— 可直接用于 Docker `HEALTHCHECK` 或就绪探针。

Kubernetes：

```yaml
readinessProbe:
  grpc: { port: 9527 }
livenessProbe:
  grpc: { port: 9527 }
```

**优雅停机会先把状态置为 `NOT_SERVING` 再停服务**，此时端口仍在监听、在途请求
继续跑完，但负载均衡器已经可以据此摘除本节点。反过来做的话是先掐连接、
上游才后知后觉。

可通过 `[MONITOR].health_check = false` 关闭（关闭后该服务不注册，
探活会收到 `UNIMPLEMENTED`）。

在代码里探活（P4 集群的节点探活也用这个）：

```python
from ipclick.health import check_health

healthy, status = check_health("10.0.0.1:9527", timeout=3)
```

### 链路记录与统计

每个请求处理完记一条：谁处理的、用了哪个适配器、状态码、耗时、重试几次、
在限流闸门里排了多久。不依赖任何第三方服务。

```toml
[TRACE]
memory_size = 500          # 内存环形缓冲，始终开启，零磁盘
sqlite_enabled = false     # 落盘（默认关）。开了 Web 端才能查历史与跨天统计
sqlite_path = "ipclick-trace.db"
retention_days = 30        # 超期的每小时清一次；0 = 永久
only_errors = false        # 只记失败请求（成功量极大时省磁盘）
record_url = true          # 关掉后只记 host
```

两层结构是刻意的：

* **内存环形缓冲**始终开着，回答"刚才发生了什么"。上限固定、零磁盘、进程重启即丢。
* **SQLite** 默认关，回答"上周三那批任务成功率多少"。WAL 模式下读不阻塞写。

写盘走**单写线程 + 有界队列**：SQLite 同一时刻只允许一个写者，让 N 个 gRPC
worker 各自去写等于在热路径上抢锁。请求线程只做一次 `put_nowait`，队列满了就丢
并计数——可观测性数据绝不能反压业务请求。**丢弃条数在 Web 端显眼地显示**，
静默丢弃会让"没有记录"和"没发生过"混为一谈。

响应里也带一份链路信息（`TaskResp.trace`）：

```python
resp = downloader.get("https://example.com")
print(resp.raw.trace.node_id)    # 真正执行的节点（集群转发时是关键信息）
print(resp.raw.trace.adapter)    # 实际用的适配器（browser 会解析成具体引擎名）
print(resp.raw.trace.attempts)   # 内部重试了几次
print(resp.raw.trace.forwarded)  # 是否经由其他节点转发
print(resp.raw.trace.queued_ms)  # 在限流闸门里排了多久
```

它刻意**不含**请求头、cookie、请求体、代理串——那些里面有机密，而这些记录是要
在 Web 端展示的。（0.3 之前这里回传的是整个原始请求 `original_request`，代理账号
密码会随之泄漏，且响应体积翻倍。该字段已移除，编号保留不复用。）

> 为什么不用 Prometheus：它按设计不保留单条记录（把 URL 放进标签会造成基数
> 爆炸），所以回答不了"我刚才那个请求为什么 403"。而这个库的使用场景恰恰是后者。
> 聚合数字这边也有——进程内计数器实时累加，代价只是重启归零。

### 安全配置

服务端会代替调用方请求任意 URL。**若监听在公网或不可信网络，请开启内网拦截**，
否则本服务可被当作内网跳板（SSRF）。

| 配置项                                 | 说明                       | 默认值                  |
|-------------------------------------|--------------------------|----------------------|
| `SECURITY.auth_token`               | 鉴权令牌，留空 = 不鉴权；可为列表以支持轮换 | `""`                 |
| `SECURITY.allowed_schemes`          | 允许的 URL 协议               | `["http", "https"]`  |
| `SECURITY.block_metadata_endpoints` | 拦截云元数据地址（169.254.169.254 等） | `true`               |
| `SECURITY.block_private_networks`   | 拦截回环 / 私网 / 保留地址         | `false`              |
| `SECURITY.allowlist`                | 即便开启拦截也放行的主机名或 IP        | `[]`                 |

### 下载器配置

这些是**请求未显式传参时**的默认值；单次请求传的 `timeout` / `max_retries` /
`retry_backoff` 优先级更高。

| 配置项                                    | 说明                          | 默认值                       |
|----------------------------------------|-----------------------------|---------------------------|
| `DOWNLOADER.connect_timeout`           | 连接超时（秒）                     | `10`                      |
| `DOWNLOADER.download_timeout`          | 下载超时（秒）                     | `300`                     |
| `DOWNLOADER.trust_env`                 | 是否读取环境变量里的代理（`HTTP_PROXY` 等） | `false`                   |
| `DOWNLOADER.retry.max_attempts`        | 最大重试次数（`0` = 不重试）           | `3`                       |
| `DOWNLOADER.retry.backoff_exponent`    | 退避指数                        | `2.0`                     |
| `DOWNLOADER.retry.initial_backoff`     | 初始等待（秒）                     | `1`                       |
| `DOWNLOADER.retry.max_backoff`         | 单次等待上限（秒，硬上限 300）           | `30`                      |
| `DOWNLOADER.retry.retry_codes`         | 触发重试的状态码；连接层异常总是会重试         | `[429,500,502,503,504]`   |
| `DOWNLOADER.concurrency.max_connections` | 连接池总上限                    | `100`                     |
| `DOWNLOADER.concurrency.max_keepalive_connections` | 长连接上限            | `20`                      |

等待时间 = `initial_backoff × backoff_exponent^已重试次数`，封顶到 `max_backoff`，
再乘一个 0.8~1.2 的抖动因子（避免并发任务集体重试造成惊群）。

### 代理配置

| 配置项                    | 说明          |
|------------------------|-------------|
| `PROXY.scheme`         | 协议，默认 `http` |
| `PROXY.host` / `.port` | 代理地址与端口     |
| `PROXY.auth_key`       | 认证用户名       |
| `PROXY.auth_password`  | 认证密码        |
| `PROXY.tunnel_server`  | 隧道服务器（覆盖 host:port） |

## 🐳 Docker 部署

构建上下文是**仓库根目录**，不是 `docker/` 目录：

```bash
docker build -f docker/Dockerfile -t ipclick:latest .
```

运行：

```bash
docker run -d -p 9527:9527 --name ipclick ipclick:latest
```

挂载自定义配置：

```bash
docker run -d -p 9527:9527 --name ipclick -v /你的路径/:/home/ipclick/.ipclick/ ipclick:latest
```

## 📚 API 参考

### DownloadTask / request() 参数

| 参数                  | 类型                     | 默认值          | 说明          |
|---------------------|------------------------|--------------|-------------|
| `url`               | `str`                  | —            | 请求 URL（必填）  |
| `method`            | `HttpMethod`           | `GET`        | HTTP 方法     |
| `adapter`           | `IPClickAdapter / str` | `CURL_CFFI`  | 使用的适配器      |
| `headers`           | `dict`                 | `None`       | 请求头         |
| `cookies`           | `dict / str`           | `None`       | Cookies     |
| `params`            | `dict`                 | `None`       | URL 查询参数    |
| `data`              | `Any`                  | `None`       | 表单数据        |
| `json`              | `dict`                 | `None`       | JSON 数据     |
| `proxy`             | `ProxyConfig/str/bool` | `None`       | 代理配置        |
| `timeout`           | `float`                | `60`         | 超时时间（秒）     |
| `max_retries`       | `int`                  | `3`          | 最大重试次数      |
| `retry_backoff`     | `float`                | `2.0`        | 重试退避基数（秒）   |
| `verify`            | `bool`                 | `True`       | 是否验证 SSL 证书 |
| `allow_redirects`   | `bool`                 | `True`       | 是否跟随重定向     |
| `impersonate`       | `str`                  | `chrome`     | 浏览器指纹伪装     |
| `allowed_status_codes` | `list[int]`         | `[200, 404]` | 不触发重试的状态码   |

### DownloadResponse 属性

| 属性             | 类型      | 说明             |
|----------------|---------|----------------|
| `status_code`  | `int`   | HTTP 状态码，`-1` 表示本地/传输失败 |
| `headers`      | `dict`  | 响应头            |
| `content`      | `bytes` | 响应内容（二进制）      |
| `text`         | `str`   | 响应内容（文本）       |
| `url`          | `str`   | 最终 URL         |
| `elapsed_ms`   | `int`   | 请求耗时（毫秒）       |
| `adapter_type` | `str`   | 实际使用的适配器名称     |
| `error`        | `str`   | 错误信息           |

### DownloadResponse 方法

- `json()` - 解析 JSON 响应
- `is_success()` / `ok` - 判断请求是否成功
- `raise_for_status()` - 状态码异常时抛出 `RequestError`

### 异常

所有异常都继承自 `IPClickError`。**注意抛出位置** —— 服务端抛的异常不会原样
传到客户端（gRPC 之间只传状态码和文本）：

| 异常                  | 场景                    | 客户端能否 `except` 到 |
|---------------------|-----------------------|---------------|
| `ValidationError`   | 任务参数校验失败，如 URL 为空、适配器名拼错（同时继承 `ValueError`） | ✅ 会抛出 |
| `TransportError`    | 与服务端的 gRPC 通信失败       | ✅ 由 `download()` 抛出 |
| `AuthenticationError` | 鉴权令牌缺失或不正确        | ✅ 会抛出（非 TransportError） |
| `ClientClosedError` | 在已关闭的客户端上继续发请求      | ✅ 会抛出（非 TransportError） |
| `RequestError`      | 目标站点返回错误，由 `raise_for_status()` 抛出 | ✅ 需自行调用 |
| `ConfigError`       | 配置缺失或非法               | ✅ 加载配置时抛出 |
| `AdapterError`      | 适配器不存在或依赖未安装          | ❌ 仅服务端 |
| `URLNotAllowedError`| 目标 URL 被安全策略拒绝        | ❌ 仅服务端 |

标 ❌ 的两个只在**服务端**抛出，客户端看到的是
`status_code == -1` 且 `error` 含说明文字的 `DownloadResponse`，
写 `except AdapterError:` 永远不会命中。

```python
resp = downloader.get(url)          # 网络失败不抛异常
if not resp.is_success():
    print(resp.error)               # 服务端拒绝的原因在这里
```

而参数错误是会抛的：

```python
downloader.get(url, adapter="htttpx")   # ValidationError: 未知的适配器名称
```

## 🚧 尚未实现 / 已知限制

为免误导，这里如实列出**当前还没有实现**的部分。配置文件里出现但落在本节的项，
改了不会有任何效果。

### 配置节生效情况

| 配置节 | 状态 |
|---|---|
| `[SERVER]` `[CLIENT]` `[PROXY]` `[LOG]` `[SECURITY]` `[DOWNLOADER]` `[MONITOR]` `[BROWSER]` `[TRACE]` `[WEB]` | ✅ 全部生效 |
| `[GENERAL]` | ✅ `debug` 与 `mode` 都生效 |
| `[CLUSTER]` | ⚠️ 除 `db_uri`（预留给 etcd/Consul）之外全部生效 |

> `[DOWNLOADER]` 的分块下载（`chunk`）、临时存储（`storage`）尚未实现，已从默认
> 配置移除；`rate_limit` 的 `redis` 后端已在 0.3.0 移除（集群限流由入口节点计算）。
> `[LOG].format` 在 0.3.0 起真正生效（此前从未被读取）。

### 功能

- **服务发现只支持 DNS**：`[CLUSTER.discovery]` 支持 `static` 与 `dns`。
  etcd / Consul 的原生 API 没接（Consul 可以用它的 DNS 接口），
  `[CLUSTER].db_uri` 仍是预留配置。
- **`undetected_chromedriver` 未实现**：它基于 selenium + chromedriver，能力与
  patchright / camoufox 高度重叠，收益不足以抵消维护成本。请求到会抛 `AdapterError`。
- **分块下载与临时存储**：`[DOWNLOADER]` 里的 `chunk` / `storage` 尚未实现。
  流式通路已经就绪，这两项可以在其上实现，但目前还没做。
- **集群流式的中途重连**：`ClusterDownloader.stream()` 只有**建流**这一步会故障
  转移。流建立之后中途断掉不会自动重连——那需要断点续传（Range 请求）才能不重复
  数据。批量则是整批发给同一个节点，不跨节点拆分。
- **文件上传**：协议里没有 multipart 字段（0.3.0 起 `files` 参数已从 API 移除，
  因为它一直是抛 `NotImplementedError` 的）。要上传文件请自己拼好 multipart 体，
  用 `data=<bytes>` 加上 `Content-Type: multipart/form-data; boundary=...` 发出去——
  `data` 现在是 bytes 字段，任意二进制都能原样送达。
- **Cookie 持久化**：请求之间不共享 cookie jar，每次请求相互独立。


## 🗺️ 路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| **P1** 打通存量 | `[DOWNLOADER]` 生效、参数错误不再伪装成网络失败、`NO_PROXY` 处理、`[GENERAL].debug` | ✅ 已完成 |
| **P2** 安全与可运维 | 服务端 token 鉴权 ✅、gRPC 标准健康检查 ✅、链路记录与统计 ✅ | ✅ 已完成 |
| **P3** 能力扩展 | 异步客户端 ✅、批量 RPC ✅、真流式下载 ✅（限速 / 分块仍待做） | ✅ 已完成 |
| **P4** 集群 | 节点池 ✅、负载均衡 ✅、健康探测 ✅、故障转移 ✅、只读状态页 ✅ | ✅ 已完成 |
| **P5** 适配器 | `niquests` ✅、`playwright` ✅（连带 `[BROWSER]` 与浏览器渲染） | ✅ 已完成 |
| **P6** 限流与引擎 | 按 host 并发 / QPS 限制 ✅、浏览器引擎可插拔（camoufox / patchright / DrissionPage）✅ | ✅ 已完成 |
| **P7** 生产化 | TLS/mTLS ✅、断点续传 ✅、DNS 服务发现 ✅、客户端重试 ✅、`[GENERAL].mode` ✅ | ✅ 已完成 |
| **P8** 打磨 | 轻量安装 ✅、niquests ✅、`--example` ✅、`.env` ✅、Web 管理端 ✅ | ✅ 已完成 |
| **P9** 分布式与可观测 | 服务端转发集群 ✅、派生式节点鉴权 ✅、链路记录 ✅、请求压缩 ✅、Web 端配置写回 / 请求流 / 试一试 ✅ | ✅ 已完成 |
| **P10** 待定 | 异步服务端、multipart 文件上传、Cookie 持久化、etcd/Consul 原生发现 | 计划中 |

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
