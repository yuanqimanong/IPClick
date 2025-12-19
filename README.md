# IPClick

![IPClick Logo](https://i.imgur.com/XvemBlO.png)

> IPClick 名字灵感来源于动画《Link Click》（时光代理人）。正如时光代理人穿梭于不同的时空执行任务，IPClick 帮助您将 HTTP 请求分发到不同的节点高效执行。

## 📖 简介

IPClick 是一个轻量级、高性能的分布式 HTTP 请求代理工具，基于 gRPC 协议构建。它提供了统一的请求接口，支持多种 HTTP
客户端适配器，帮助开发者更高效地处理网络请求。

## ✨ 特性

- **多适配器支持**：内置 `curl_cffi`、`httpx`、`requests` 等多种 HTTP 客户端适配器
- **浏览器指纹伪装**：基于 `curl_cffi` 实现浏览器指纹模拟，有效绑过反爬检测
- **gRPC 通信**：使用 gRPC 协议进行高效的客户端-服务端通信
- **代理支持**：灵活的代理配置，支持 HTTP/HTTPS/SOCKS 代理
- **自动重试**：内置请求重试机制，支持自定义重试策略和退避算法
- **命令行工具**：提供便捷的 CLI 工具，支持快速启动服务和测试请求
- **Docker 支持**：提供 Dockerfile，支持容器化部署
- **易于扩展**：模块化设计，便于添加新的适配器和功能

## 📦 安装

### 从 PyPI 安装

```bash
pip install ipclick
```

### 从源码安装

```bash
# 克隆仓库
git clone https://github.com/yuanqimanong/IPClick.git
cd IPClick

# 使用 Poetry 安装依赖
poetry install

# 或使用 pip 安装
pip install -e .
```

## 🔧 系统要求

- Python >= 3.14
- 依赖库：
    - curl-cffi >= 0.13.0
    - grpcio >= 1.76.0
    - protobuf >= 6.33.2
    - click >= 8.3.1
    - httpx >= 0.28.1
    - fake-useragent >= 2.2.0

## 🚀 快速开始

### 启动服务端

```bash
# 使用默认配置启动
ipclick run

# 指定端口和地址
ipclick run --host 0.0.0.0 --port 9527

# 使用自定义配置文件
ipclick run --config /path/to/config.yaml

# 显示详细日志
ipclick run --verbose
```

### 客户端使用

```python
from ipclick import Downloader, HttpMethod

# 创建下载器实例
downloader = Downloader()

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
    timeout=30
)

# 检查请求是否成功
if response.is_success():
    print(f"请求成功，耗时:  {response.elapsed_ms}ms")
else:
    print(f"请求失败: {response.error}")
```

### 使用代理

```python
from ipclick import Downloader, ProxyConfig, HttpMethod

# 方式一：使用代理配置对象
proxy = ProxyConfig(
    scheme="http",
    host="proxy.example.com",
    port=8080,
    auth_key="username",
    auth_password="password"
)

response = downloader.request(
    method=HttpMethod.GET,
    url="https://httpbin.org/ip",
    proxy=proxy
)

# 方式二：使用代理 URL 字符串
response = downloader.request(
    method=HttpMethod.GET,
    url="https://httpbin.org/ip",
    proxy="http://user:pass@proxy.example.com:8080"
)

# 方式三：使用配置文件中的代理（设置 proxy=True）
response = downloader.request(
    method=HttpMethod.GET,
    url="https://httpbin.org/ip",
    proxy=True
)
```

### 使用全局下载器

```python
from ipclick import downloader

# 使用默认的全局下载器实例
response = downloader.get("https://httpbin.org/ip")
print(response.text)
```

## 📂 项目结构

```
IPClick/
├── src/
│   └── ipclick/
│       ├── __init__.py          # 包入口，导出公共 API
│       ├── __main__.py          # 模块入口
│       ├── sdk. py               # SDK 客户端实现
│       ├── server.py            # gRPC 服务端实现
│       ├── adapters/            # HTTP 客户端适配器
│       │   ├── base.py          # 适配器基类
│       │   ├── curl_cffi_adapter.py
│       │   └── httpx_adapter.py
│       ├── cli/                 # 命令行工具
│       │   └── main.py
│       ├── config_loader/       # 配置加载器
│       ├── configs/             # 默认配置文件
│       ├── dto/                 # 数据传输对象
│       │   ├── models.py        # 数据模型定义
│       │   ├── response.py      # 响应对象
│       │   └── proto/           # Protobuf 定义
│       ├── services/            # gRPC 服务实现
│       └── utils/               # 工具模块
├── docker/                      # Docker 相关文件
│   └── Dockerfile
├── examples/                    # 示例代码
├── tests/                       # 测试代码
├── pyproject.toml              # 项目配置
└── README. md
```

## ⚙️ 配置说明

### 服务端配置

| 配置项                  | 说明      | 默认值       |
|----------------------|---------|-----------|
| `SERVER. host`       | 服务绑定地址  | `0.0.0.0` |
| `SERVER.port`        | 服务端口    | `9527`    |
| `SERVER.max_workers` | 最大工作线程数 | `10`      |

### 客户端配置

| 配置项                        | 说明        | 默认值    |
|----------------------------|-----------|--------|
| `DOWNLOADER.timeout`       | 默认超时时间（秒） | `60`   |
| `DOWNLOADER.max_retries`   | 最大重试次数    | `3`    |
| `DOWNLOADER.retry_backoff` | 重试退避时间（秒） | `2. 0` |

## 🐳 Docker 部署

```bash
# 构建镜像
cd docker
docker build -t ipclick .

# 运行容器
docker run -d -p 9527:9527 --name ipclick ipclick
```

## 📚 API 参考

### DownloadTask 参数

| 参数                | 类型                     | 说明         |
|-------------------|------------------------|------------|
| `url`             | `str`                  | 请求 URL（必填） |
| `method`          | `HttpMethod`           | HTTP 方法    |
| `headers`         | `Dict`                 | 请求头        |
| `cookies`         | `Dict/str`             | Cookies    |
| `params`          | `Dict`                 | URL 查询参数   |
| `data`            | `Any`                  | 表单数据       |
| `json`            | `Dict`                 | JSON 数据    |
| `proxy`           | `ProxyConfig/str/bool` | 代理配置       |
| `timeout`         | `float`                | 超时时间（秒）    |
| `max_retries`     | `int`                  | 最大重试次数     |
| `verify`          | `bool`                 | 是否验证 SSL   |
| `allow_redirects` | `bool`                 | 是否跟随重定向    |
| `impersonate`     | `str`                  | 浏览器指纹伪装    |

### DownloadResponse 属性

| 属性            | 类型      | 说明        |
|---------------|---------|-----------|
| `status_code` | `int`   | HTTP 状态码  |
| `headers`     | `Dict`  | 响应头       |
| `content`     | `bytes` | 响应内容（二进制） |
| `text`        | `str`   | 响应内容（文本）  |
| `url`         | `str`   | 最终 URL    |
| `elapsed_ms`  | `int`   | 请求耗时（毫秒）  |
| `error`       | `str`   | 错误信息      |

### DownloadResponse 方法

- `json()` - 解析 JSON 响应
- `is_success()` - 判断请求是否成功
- `raise_for_status()` - 状态码异常时抛出异常

## 🤝 贡献

欢迎贡献代码、报告问题或提出建议！请通过 [GitHub Issues](https://github.com/yuanqimanong/IPClick/issues)
或 [Pull Requests](https://github.com/yuanqimanong/IPClick/pulls) 参与项目开发。

## 📄 许可证

本项目采用 [MIT License](LICENSE) 开源许可证。

Copyright (c) 2025 元气码农少女酱