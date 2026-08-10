# IPClick

![IPClick Logo](https://raw.githubusercontent.com/yuanqimanong/IPClick/master/docs/logo.png)

> IPClick 名字灵感来源于动画《Link Click》（时光代理人）。正如时光代理人穿梭于不同的时空执行任务，IPClick 帮助您将 HTTP 请求分发到不同的节点高效执行。

## 📖 简介

IPClick 是一个轻量级、高性能的分布式 HTTP 请求代理工具，基于 gRPC 协议构建。它提供了统一的请求接口，支持多种 HTTP
客户端适配器，帮助开发者更高效地处理网络请求。

## ✨ 特性

- **多适配器支持**：内置 `curl_cffi`、`httpx`、`requests`、`playwright` 适配器，并可注册自定义适配器
- **浏览器指纹伪装**：基于 `curl_cffi` 实现浏览器指纹模拟，有效绕过反爬检测
- **浏览器渲染**：起真实浏览器执行 JS，四个引擎可选（camoufox / patchright /
  playwright / DrissionPage），默认按平台挑
- **集群与故障转移**：多节点客户端，支持轮询 / 随机 / 加权均衡、健康探测与自动换节点
- **gRPC 通信**：使用 gRPC 协议进行高效的客户端-服务端通信
- **连接复用**：客户端复用 gRPC channel，服务端复用适配器与 HTTP 连接池
- **代理支持**：灵活的代理配置，支持 HTTP/HTTPS 代理
- **自动重试**：内置请求重试机制，支持指数退避 + 抖动，并可按状态码重试
- **按 host 限流**：服务端按目标域名严格限制并发数与 QPS，避免把单个站点打爆
- **流式下载**：大文件分片传输，服务端与客户端都不需要把整个响应体驻留内存
- **批量请求**：一次 RPC 处理多个任务，结果按完成顺序流式返回
- **异步客户端**：基于 `grpc.aio` 的 `AsyncDownloader`，与同步版接口对应
- **令牌鉴权**：gRPC 标准 Bearer 令牌，支持环境变量注入与多令牌轮换
- **健康检查**：实现 `grpc.health.v1` 标准协议，K8s 探针与服务网格开箱即用
- **Prometheus 指标**：请求量 / 延迟 / 重试 / 拒绝等指标，可选依赖、优雅降级
- **SSRF 防护**：服务端对目标 URL 做协议白名单与内网/元数据地址拦截
- **命令行工具**：提供便捷的 CLI 工具，支持快速启动服务和查看配置
- **Docker 支持**：多阶段构建、非 root 运行的镜像
- **完整类型标注**：随包提供 `py.typed`，下游可直接享受类型检查

## 📦 安装

### 从 PyPI 安装

```bash
pip install ipclick
```

可选功能按需安装：

```bash
pip install "ipclick[metrics]"      # Prometheus 指标
pip install "ipclick[requests]"     # requests 适配器
pip install "ipclick[camoufox]"     # 浏览器渲染：Firefox 反检测（Linux/macOS 默认）
pip install "ipclick[drissionpage]" # 浏览器渲染：CDP 直连（Windows 默认）
pip install "ipclick[patchright]"   # 浏览器渲染：Chromium 反检测
pip install "ipclick[browser]"      # 浏览器渲染：原版 playwright
pip install "ipclick[all]"          # 以上全部
```

浏览器引擎装完 Python 包之后还要准备浏览器本体：

```bash
python -m camoufox fetch        # camoufox：下载它自己的 Firefox
patchright install chromium     # patchright
playwright install chromium     # playwright
# DrissionPage 用本机已装的 Chrome/Chromium，不用额外下载
```

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
- 主要依赖：
    - curl-cffi >= 0.16.0
    - grpcio >= 1.83.0
    - protobuf >= 6.33.2
    - click >= 8.4.2
    - httpx >= 0.28.1
    - fake-useragent >= 2.2.0
    - loguru >= 0.7.3
    - python-box >= 7.4.1
    - uuid-utils >= 0.17.0

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

查看当前生效的配置：

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

response = downloader.get("https://httpbin.org/get", adapter=IPClickAdapter.HTTPX)
```

| 适配器 | 反检测 | JS 渲染 | 安装 |
|---|---|---|---|
| `curl_cffi`（默认） | TLS 指纹伪装 | ❌ | 内置 |
| `httpx` | ❌ | ❌ | 内置 |
| `requests` | ❌ | ❌ | `ipclick[requests]` |
| `browser` | 由服务端引擎决定 | ✅ | 见下 |
| `camoufox` | Firefox + 完整指纹伪装 | ✅ | `ipclick[camoufox]` |
| `patchright` | Chromium，Playwright 反检测分支 | ✅ | `ipclick[patchright]` |
| `playwright` | ❌（原版，最稳） | ✅ | `ipclick[browser]` |
| `DrissionPage` | Chromium，CDP 直连 | ✅ | `ipclick[drissionpage]` |

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

> ⚠️ **这会占用 worker 线程。** 服务端是一请求一线程，排队等额度会占着那个
> 线程。`per_host_max_concurrent` 设得很小、同时又有大量请求打向同一个 host 时，
> 线程池会被排队的请求占满，其他 host 的请求也跟着饿死。所以等待有硬性上限
> （`per_host_wait_timeout`），并且 `[SERVER].max_workers` 要留够余量。
> 真要做到"排队不占线程"，得把服务端改成 `grpc.aio`，那是另一件事。

超时会抛 `HostLimitTimeout`，而不是返回 `status_code == -1`：这是本机的限流策略
生效了，不是网络故障，返回 -1 会让人去排查目标站点。

### 集群与故障转移

`ClusterDownloader` 把请求分发到多个 IPClick 服务端，接口与单节点的
`Downloader` 一致：

```python
from ipclick.cluster import ClusterDownloader

with ClusterDownloader() as d:          # 节点取自 [CLUSTER].nodes
    resp = d.get("https://example.com")
    print(d.snapshot())                 # 各节点健康状态
```

```toml
[CLUSTER]
load_balancer = "round_robin"   # round_robin / random / weight
probe_interval = 10             # 健康探测间隔（秒）
failure_threshold = 3           # 连续失败多少次判定摘除
recovery_threshold = 2          # 连续成功多少次判定恢复
max_failover = 2                # 单个请求最多换几个节点

[[CLUSTER.nodes]]
id = "node-a"
address = "10.0.0.1:9527"
weight = 100
region = "cn-east"
```

摘除与恢复都用**连续计数阈值**而不是单次结果：一次网络抖动就摘节点会让流量
反复横跳。探测走 `grpc.health.v1`，也就是 P2 那套标准健康检查。

故障转移只在 `TransportError`（这个节点有问题）时发生。参数错误、鉴权失败换个
节点还是一样的结果，直接上抛，不浪费尝试次数。

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
│       ├── metrics.py           # Prometheus 指标（可选依赖）
│       ├── limiter.py           # 按 host 的并发与 QPS 闸门
│       ├── exceptions.py        # 异常层次
│       ├── py.typed             # 类型标注标记
│       ├── adapters/            # 下载器适配器
│       │   ├── base.py          # 适配器基类与重试装饰器
│       │   ├── settings.py      # [DOWNLOADER] 配置
│       │   ├── browser_settings.py  # [BROWSER] 配置
│       │   ├── browser_engines.py   # 引擎选择与启动（含平台默认）
│       │   ├── curl_cffi_adapter.py
│       │   ├── httpx_adapter.py
│       │   ├── requests_adapter.py
│       │   ├── browser_adapter.py   # playwright / patchright / camoufox
│       │   ├── drission_adapter.py  # DrissionPage（CDP 直连）
│       │   └── registry.py      # 适配器注册表
│       ├── cluster/             # 集群客户端
│       │   ├── node.py          # 节点模型与 [CLUSTER] 配置
│       │   ├── balancer.py      # 轮询 / 随机 / 加权
│       │   ├── pool.py          # 节点池与健康探测
│       │   ├── client.py        # ClusterDownloader（故障转移）
│       │   └── status_page.py   # 只读状态页
│       ├── cli/                 # 命令行工具
│       ├── config_loader/       # 配置加载器
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

### Prometheus 指标

需要可选依赖：

```bash
pip install "ipclick[metrics]"
```

```toml
[MONITOR]
metrics_enabled = true
metrics_port = 9528
metrics_host = "0.0.0.0"   # 只允许本机抓取则改成 127.0.0.1
```

指标走**独立 HTTP 端口**（Prometheus 生态惯例），不复用 gRPC 端口。
默认关闭 —— 指标端点通常比业务端口设防更少，应由部署方显式决定是否开、开在哪。

| 指标 | 类型 | 标签 |
|---|---|---|
| `ipclick_requests_total` | Counter | `adapter` `method` `outcome` |
| `ipclick_request_duration_seconds` | Histogram | `adapter` |
| `ipclick_response_bytes` | Histogram | `adapter` |
| `ipclick_requests_in_flight` | Gauge | — |
| `ipclick_retries_total` | Counter | `adapter` `reason` |
| `ipclick_rejected_total` | Counter | `reason` |
| `ipclick_build_info` | Info | `version` |

`outcome` 取值为 `2xx` / `3xx` / `4xx` / `5xx` / `failure`（`failure` 表示连接层
失败，压根没拿到 HTTP 响应）。`reason` 取值为 `unauthenticated` /
`url_not_allowed` / `invalid_argument` / `internal_error` / `exception` /
`status_code`。

> **指标标签里不含目标 URL 或主机名，这是刻意的。** 爬虫场景下目标是无界的，
> 用它做标签会让 Prometheus 的时间序列数量爆炸；而且指标端点往往设防更少，
> 把抓取目标暴露在那里等于公开业务意图。需要按站点分析请走日志，不要走指标。

未安装 `prometheus_client` 时所有埋点降级为无操作，功能不受任何影响。

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
| `[SERVER]` `[PROXY]` `[LOG]` `[SECURITY]` `[DOWNLOADER]` `[MONITOR]` `[BROWSER]` | ✅ 生效 |
| `[GENERAL]` | ⚠️ `debug` 生效；`mode` 无消费方 |
| `[CLUSTER]` | ⚠️ `load_balancer` / `nodes` / 阈值生效；`db_uri` 无消费方 |

> `[DOWNLOADER]` 的分块下载（`chunk`）、临时存储（`storage`）尚未实现，已从默认
> 配置移除。限速（`rate_limit`）已在 P6 实现，见「按 host 限流」。

### 功能

- **服务发现**：集群的节点列表来自静态配置，改了要重启。没有 DNS / etcd / Consul
  之类的动态发现，`[CLUSTER].db_uri` 仍是预留配置。
- **传输加密 / mTLS**：已支持令牌鉴权，但 gRPC 仍是 `insecure_channel`（明文）。
  令牌在不受信任的网络上会被窃听，请在 TLS 终端（如 nginx / service mesh）之后部署，
  或等待后续的 mTLS 支持。
- **`undetected_chromedriver` 未实现**：它基于 selenium + chromedriver，能力与
  patchright / camoufox 高度重叠，收益不足以抵消维护成本。请求到会抛 `AdapterError`。
- **分块下载与临时存储**：`[DOWNLOADER]` 里的 `chunk` / `storage` 尚未实现。
  流式通路已经就绪，这两项可以在其上实现，但目前还没做。
- **限流只在单机内生效**：`per_host_max_concurrent` / `per_host_qps` 是每个服务端
  进程各自计数的。集群部署时 N 个节点就是 N 倍的实际并发，没有跨节点的共享计数器
  （那需要 Redis 之类的外部状态）。
- **集群流式的中途重连**：`ClusterDownloader.stream()` 只有**建流**这一步会故障
  转移。流建立之后中途断掉不会自动重连——那需要断点续传（Range 请求）才能不重复
  数据。批量则是整批发给同一个节点，不跨节点拆分。
- **文件上传**：`files` 参数会抛 `NotImplementedError`。
- **Cookie 持久化**：请求之间不共享 cookie jar，每次请求相互独立。
- **客户端重试**：重试只发生在服务端适配器内部；客户端到服务端这一跳失败不会重试。


## 🗺️ 路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| **P1** 打通存量 | `[DOWNLOADER]` 生效、参数错误不再伪装成网络失败、`NO_PROXY` 处理、`[GENERAL].debug` | ✅ 已完成 |
| **P2** 安全与可运维 | 服务端 token 鉴权 ✅、gRPC 标准健康检查 ✅、Prometheus metrics ✅ | ✅ 已完成 |
| **P3** 能力扩展 | 异步客户端 ✅、批量 RPC ✅、真流式下载 ✅（限速 / 分块仍待做） | ✅ 已完成 |
| **P4** 集群 | 节点池 ✅、负载均衡 ✅、健康探测 ✅、故障转移 ✅、只读状态页 ✅ | ✅ 已完成 |
| **P5** 适配器 | `requests` ✅、`playwright` ✅（连带 `[BROWSER]` 与浏览器渲染） | ✅ 已完成 |
| **P6** 限流与引擎 | 按 host 并发 / QPS 限制 ✅、浏览器引擎可插拔（camoufox / patchright / DrissionPage）✅ | ✅ 已完成 |
| **P7** 待定 | mTLS、服务发现、分块下载、Cookie 持久化 | 计划中 |

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
