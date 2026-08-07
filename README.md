# IPClick

![IPClick Logo](https://i.imgur.com/XvemBlO.png)

> IPClick 名字灵感来源于动画《Link Click》（时光代理人）。正如时光代理人穿梭于不同的时空执行任务，IPClick 帮助您将 HTTP 请求分发到不同的节点高效执行。

## 📖 简介

IPClick 是一个轻量级、高性能的分布式 HTTP 请求代理工具，基于 gRPC 协议构建。它提供了统一的请求接口，支持多种 HTTP
客户端适配器，帮助开发者更高效地处理网络请求。

## ✨ 特性

- **多适配器支持**：内置 `curl_cffi`、`httpx` 适配器，并可注册自定义适配器
- **浏览器指纹伪装**：基于 `curl_cffi` 实现浏览器指纹模拟，有效绕过反爬检测
- **gRPC 通信**：使用 gRPC 协议进行高效的客户端-服务端通信
- **连接复用**：客户端复用 gRPC channel，服务端复用适配器与 HTTP 连接池
- **代理支持**：灵活的代理配置，支持 HTTP/HTTPS 代理
- **自动重试**：内置请求重试机制，支持指数退避 + 抖动，并可按状态码重试
- **SSRF 防护**：服务端对目标 URL 做协议白名单与内网/元数据地址拦截
- **命令行工具**：提供便捷的 CLI 工具，支持快速启动服务和查看配置
- **Docker 支持**：多阶段构建、非 root 运行的镜像
- **完整类型标注**：随包提供 `py.typed`，下游可直接享受类型检查

## 📦 安装

### 从 PyPI 安装

```bash
pip install ipclick
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

## 📂 项目结构

```
IPClick/
├── src/
│   └── ipclick/
│       ├── __init__.py          # 包入口，导出公共 API
│       ├── __main__.py          # 模块入口
│       ├── sdk.py               # SDK 客户端实现
│       ├── server.py            # gRPC 服务端实现
│       ├── exceptions.py        # 异常层次
│       ├── py.typed             # 类型标注标记
│       ├── adapters/            # HTTP 客户端适配器
│       │   ├── base.py          # 适配器基类与重试装饰器
│       │   ├── curl_cffi_adapter.py
│       │   ├── httpx_adapter.py
│       │   └── registry.py      # 适配器注册表
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

### 安全配置

服务端会代替调用方请求任意 URL。**若监听在公网或不可信网络，请开启内网拦截**，
否则本服务可被当作内网跳板（SSRF）。

| 配置项                                 | 说明                       | 默认值                  |
|-------------------------------------|--------------------------|----------------------|
| `SECURITY.allowed_schemes`          | 允许的 URL 协议               | `["http", "https"]`  |
| `SECURITY.block_metadata_endpoints` | 拦截云元数据地址（169.254.169.254 等） | `true`               |
| `SECURITY.block_private_networks`   | 拦截回环 / 私网 / 保留地址         | `false`              |
| `SECURITY.allowlist`                | 即便开启拦截也放行的主机名或 IP        | `[]`                 |

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

所有异常都继承自 `IPClickError`：

| 异常                  | 场景                    |
|---------------------|-----------------------|
| `ConfigError`       | 配置缺失或非法               |
| `AdapterError`      | 适配器不存在或依赖未安装          |
| `TransportError`    | 与服务端的 gRPC 通信失败       |
| `RequestError`      | 目标站点返回错误              |
| `ValidationError`   | 任务参数校验失败（同时继承 `ValueError`） |
| `URLNotAllowedError`| 目标 URL 被安全策略拒绝        |

## 🚧 尚未实现 / 已知限制

为免误导，这里如实列出**当前还没有实现**的部分。配置文件里出现但落在本节的项，
改了不会有任何效果。

### 配置节：仅 4 个真正生效

| 配置节 | 状态 |
|---|---|
| `[SERVER]` `[PROXY]` `[LOG]` `[SECURITY]` | ✅ 生效 |
| `[GENERAL]` | ❌ `mode` / `debug` 无消费方 |
| `[CLUSTER]` | ❌ `load_balancer` / `nodes` / `db_uri` 无消费方 |
| `[DOWNLOADER]` | ❌ `connect_timeout` / `download_timeout` / `retry.*` / `concurrency.*` / `rate_limit.*` / `chunk.*` / `storage.*` 全部无消费方 |
| `[BROWSER]` | ❌ 无消费方 |
| `[MONITOR]` | ❌ `health_check` 无消费方 |

> 超时与重试目前只能**按请求**传参（`timeout` / `max_retries` / `retry_backoff`），
> 配置文件里的对应项不生效。

### 功能

- **分布式 / 集群**：尽管项目定位是"分布式"，目前客户端只连**单个** `host:port`。
  没有节点池、负载均衡、故障转移或服务发现。`[CLUSTER]` 是预留配置。
- **服务端鉴权**：gRPC 使用 `insecure_channel`，**没有任何鉴权**。任何能连到端口的
  调用方都能使用本服务。部署到非可信网络时，请自行用防火墙 / 网络策略限制来源，
  并开启 `[SECURITY].block_private_networks`。
- **适配器**：`IPClickAdapter` 枚举列出 6 种，实际只实现 **`curl_cffi`** 和 **`httpx`**。
  请求 `requests` / `DrissionPage` / `undetected_chromedriver` / `playwright` 会抛
  `AdapterError`。
- **浏览器渲染**：`automation_config` / `automation_script` 字段贯穿了整条调用链，
  但末端没有任何消费方，属于预留接口。
- **流式下载**：`stream` 参数目前不改变行为。响应体经由单个 protobuf 消息整体传输
  （上限 500MB），大文件会完整驻留内存，不适合下载超大文件。
- **文件上传**：`files` 参数会抛 `NotImplementedError`。
- **批量请求**：一次 RPC 只处理一个 URL，没有批量 / 流式接口。
- **异步客户端**：只有同步 `Downloader`，没有 `grpc.aio` 版本。
- **Cookie 持久化**：请求之间不共享 cookie jar，每次请求相互独立。
- **客户端重试**：重试只发生在服务端适配器内部；客户端到服务端这一跳失败不会重试。
- **可观测性**：没有 metrics，也没有实现 gRPC 标准健康检查协议。Docker 的
  healthcheck 只是探测端口 TCP 可连。

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

本项目采用 [MIT License](LICENSE) 开源许可证。

Copyright (c) 2025 元气码农少女酱
