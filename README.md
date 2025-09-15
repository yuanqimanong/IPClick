# IPClick 代理池系统

![IPClick Logo](logo.jpeg)

> IPClick 名字灵感来源于动画《Link Click》（时光代理人）。正如时光代理人可以穿梭于不同的照片中，IPClick 让您可以通过不同的代理IP访问互联网。

IPClick 是一个功能强大、易于使用的代理池系统，支持多种代理协议（HTTP、HTTPS、SOCKS4、SOCKS5）和多种存储后端（内存、SQLite、Redis、MySQL）。它能自动获取、验证和管理代理，为您的应用提供高质量的代理服务。

## 特性

- **多协议支持**：支持 HTTP、HTTPS、SOCKS4、SOCKS5 代理
- **多存储后端**：支持内存、SQLite、Redis、MySQL 存储
- **自动检测**：定期检测代理有效性和匿名性
- **RESTful API**：提供简单易用的 API 接口
- **Web 管理界面**：直观的 Web 管理界面
- **插件系统**：可扩展的插件系统，轻松添加新的代理源
- **开箱即用**：无需额外依赖，默认使用 SQLite 存储
- **高度可配置**：通过配置文件、环境变量或命令行参数自定义行为
- **用户友好**：通过 pip 安装后，可以在用户自己的项目目录中生成配置文件
- **自定义插件**：支持用户自定义代理获取器和存储后端插件

## 系统架构

IPClick 采用模块化设计，主要由以下几个部分组成：

### 核心模块 (core)

- **代理模型 (models.py)**：定义代理数据结构
- **代理获取器 (fetcher.py)**：从各种来源获取代理
- **代理验证器 (validator.py)**：验证代理的有效性和匿名性
- **代理存储 (storage.py)**：管理代理的存储和检索
- **调度器 (scheduler.py)**：协调各个组件的工作
- **IP管理器 (ip_manager.py)**：管理IP的分配和轮询

### API 模块 (api)

- **API 服务 (server.py)**：提供 RESTful API 接口
- **Web 界面 (web.py)**：提供 Web 管理界面

### 插件模块 (plugins)

- **代理源插件 (fetchers/)**：各种代理源的实现
- **存储后端插件 (storages/)**：自定义存储后端的实现

### 工具模块 (utils)

- **HTTP 客户端 (http.py, http_client.py)**：处理 HTTP 请求
- **任务管理 (task.py)**：管理异步任务
- **插件加载器 (plugin_loader.py)**：加载和管理插件
- **代理连接器 (proxy_connector.py)**：提供代理连接功能

### 架构图

```
+----------------+      +----------------+      +----------------+
|                |      |                |      |                |
|  代理获取器     | ---> |    代理验证器   | ---> |    代理存储     |
|  (Fetcher)     |      |  (Validator)   |      |   (Storage)    |
|                |      |                |      |                |
+----------------+      +----------------+      +-------+--------+
                                                        |
                                                        v
+----------------+      +----------------+      +----------------+
|                |      |                |      |                |
|   Web 界面     | <--- |    调度器      | <--- |   API 服务     |
|  (Web UI)      |      | (Scheduler)    |      |  (API Server)  |
|                |      |                |      |                |
+----------------+      +----------------+      +----------------+
```

## 项目结构

```
/                        # 项目根目录
├── ipclick/             # 主包目录
│   ├── core/            # 核心模块
│   │   ├── __init__.py
│   │   ├── fetcher.py   # 代理获取
│   │   ├── validator.py # 代理验证
│   │   ├── storage.py   # 代理存储
│   │   ├── scheduler.py # 调度器
│   │   └── models.py    # 数据模型
│   ├── api/             # API模块
│   │   ├── __init__.py
│   │   ├── server.py    # API服务
│   │   ├── web.py       # Web UI服务
│   │   └── base_service.py # 基础服务
│   ├── plugins/         # 插件模块
│   │   ├── __init__.py
│   │   ├── fetchers/    # 代理源插件
│   │   └── storages/    # 存储后端插件
│   ├── utils/           # 工具模块
│   │   ├── __init__.py
│   │   ├── logger.py    # 日志工具
│   │   ├── http.py      # HTTP工具
│   │   ├── http_client.py # HTTP客户端
│   │   ├── task.py      # 任务管理
│   │   ├── plugin_loader.py # 插件加载器
│   │   └── proxy_connector.py # 代理连接器
│   ├── web/             # Web模块
│   │   ├── static/      # 静态资源
│   │   └── templates/   # 模板文件
│   ├── config.py        # 配置文件
│   ├── cli.py           # 命令行接口
│   └── __init__.py      # 包初始化文件
├── examples/            # 示例代码目录
│   ├── client_example.py # 客户端示例
│   ├── high_concurrency_example.py # 高并发示例
│   ├── simple_client.py # 简单客户端示例
│   └── README.md        # 示例说明文档
├── publish.py           # 发布脚本
├── main.py              # 入口文件（向后兼容）
├── setup.py             # 安装脚本
├── pyproject.toml       # 项目配置
├── MANIFEST.in          # 包含文件清单
├── requirements.txt     # 依赖文件
└── README.md            # 项目说明文档
```

## 安装与使用

### 安装

#### 从 PyPI 安装（推荐）

```bash
# 安装基础版本
pip install ipclick

# 安装带Redis支持的版本
pip install ipclick[redis]

# 安装带MySQL支持的版本
pip install ipclick[mysql]

# 安装带MongoDB支持的版本
pip install ipclick[mongodb]

# 安装完整版本（包含所有可选依赖）
pip install ipclick[all]
```

#### 从源码安装

```bash
# 克隆仓库
git clone https://github.com/yourusername/ipclick.git
cd ipclick

# 安装依赖并安装包
pip install -e .

# 安装完整版本（包含所有可选依赖）
pip install -e .[all]
```

### 初始化配置

首次使用时，建议先初始化配置文件：

```bash
# 在当前目录创建配置文件
ipclick init

# 指定配置文件路径
ipclick init --config /path/to/config.yaml

# 强制覆盖已存在的配置文件
ipclick init --force
```

### 启动服务

```bash
# 启动所有服务
ipclick start

# 仅启动调度器
ipclick start --scheduler-only

# 仅启动API服务
ipclick start --api-only

# 仅启动Web服务
ipclick start --web-only

# 指定配置文件
ipclick start --config /path/to/config.yaml

# 指定日志级别
ipclick start --log-level DEBUG
```

### 停止服务

```bash
# 停止所有服务
ipclick stop

# 指定PID文件
ipclick stop --pid-file /path/to/ipclick.pid
```

### 查看服务状态

```bash
# 查看服务状态
ipclick status
```

### 插件管理

```bash
# 列出所有插件
ipclick plugin list

# 列出代理获取器插件
ipclick plugin list --type fetcher

# 列出存储后端插件
ipclick plugin list --type storage

# 安装插件（本地文件）
ipclick plugin install /path/to/plugin.py

# 安装插件（远程URL）
ipclick plugin install https://example.com/plugin.py

# 启用插件
ipclick plugin enable plugin_name

# 禁用插件
ipclick plugin disable plugin_name
```

### 配置管理

```bash
# 查看所有配置
ipclick config get

# 查看特定配置项
ipclick config get storage.type

# 设置配置项
ipclick config set storage.type redis

# 重置配置
ipclick config reset --force
```

### 其他命令

```bash
# 查看版本信息
ipclick version

# 检查环境和依赖
ipclick check
```

### 兼容旧版本的命令

```bash
# 使用模块方式启动
python -m ipclick

# 查看帮助
python -m ipclick --help

# 仅启动调度器
python -m ipclick --scheduler-only

# 指定配置文件
python -m ipclick --config config.yaml
```

### 配置

IPClick 默认使用 SQLite 存储，无需额外配置即可运行。如果需要使用其他存储后端，可以修改 `config.py` 文件：

```python
# 存储配置
"storage": {
    "type": "sqlite",  # sqlite, memory, redis, mysql
    "max_proxies": 1000,  # 最大代理数量

    # SQLite存储配置
    "sqlite": {
        "path": "data/proxy_pool.db",  # 数据库文件路径
        "table": "proxies"  # 表名
    },

    # 内存存储配置
    "memory": {
        "persistence": True,  # 是否持久化到文件
        "persistence_file": "data/proxies.json"  # 持久化文件路径
    },

    # Redis存储配置
    "redis": {
        "host": "localhost",
        "port": 6379,
        "password": "",
        "db": 0,
        "key_prefix": "ipclick:"
    },

    # MySQL存储配置
    "mysql": {
        "host": "localhost",
        "port": 3306,
        "user": "root",
        "password": "",
        "database": "proxy_pool",
        "table": "proxies"
    }
},
```

## API 使用

IPClick 提供了 RESTful API 接口，可以通过 HTTP 请求获取代理：

### 获取随机代理

```bash
# 获取随机代理
curl http://localhost:5555/get

# 获取随机HTTP代理
curl http://localhost:5555/get/http

# 获取随机HTTPS代理
curl http://localhost:5555/get/http?https=true

# 获取随机SOCKS5代理
curl http://localhost:5555/get/socks?version=5

# 获取随机SOCKS4代理
curl http://localhost:5555/get/socks?version=4

# 获取高匿名代理
curl http://localhost:5555/get?anonymous=high_anonymous
```

### 获取代理数量

```bash
# 获取所有代理数量
curl http://localhost:5555/count

# 获取HTTP代理数量
curl http://localhost:5555/count?protocol=http

# 获取HTTPS代理数量
curl http://localhost:5555/count?protocol=https

# 获取SOCKS5代理数量
curl http://localhost:5555/count?protocol=socks5
```

### 获取所有代理

```bash
# 获取所有代理
curl http://localhost:5555/all

# 获取所有HTTP代理
curl http://localhost:5555/all?protocol=http

# 获取所有HTTPS代理
curl http://localhost:5555/all?protocol=https

# 获取所有SOCKS5代理
curl http://localhost:5555/all?protocol=socks5

# 限制返回数量
curl http://localhost:5555/all?limit=10
```

## 在代码中使用

IPClick 可以通过两种方式使用：作为独立服务或作为库集成到您的项目中。

### 作为独立服务使用

#### 1. 启动服务

```bash
# 安装IPClick
pip install ipclick

# 初始化配置文件
ipclick init

# 启动服务
ipclick start
```

#### 2. 使用 HTTP/HTTPS 代理

```python
import requests

# 获取代理
proxy_api = "http://localhost:5555/get/http"
response = requests.get(proxy_api)
proxy_data = response.json()["proxy"]

# 构建代理URL
protocol = proxy_data["protocol"]
host = proxy_data["host"]
port = proxy_data["port"]
username = proxy_data.get("username")
password = proxy_data.get("password")

# 构建代理字典
if username and password:
    proxy_url = f"{protocol}://{username}:{password}@{host}:{port}"
else:
    proxy_url = f"{protocol}://{host}:{port}"

proxies = {
    "http": proxy_url,
    "https": proxy_url
}

# 使用代理发送请求
response = requests.get("http://httpbin.org/ip", proxies=proxies)
print(response.text)
```

#### 3. 使用 SOCKS 代理

```python
import requests

# 获取代理
proxy_api = "http://localhost:5555/get/socks"
response = requests.get(proxy_api)
proxy_data = response.json()["proxy"]

# 构建代理URL
protocol = proxy_data["protocol"]
host = proxy_data["host"]
port = proxy_data["port"]
username = proxy_data.get("username")
password = proxy_data.get("password")

# 构建代理字典
if username and password:
    proxy_url = f"{protocol}://{username}:{password}@{host}:{port}"
else:
    proxy_url = f"{protocol}://{host}:{port}"

proxies = {
    "http": proxy_url,
    "https": proxy_url
}

# 使用代理发送请求
response = requests.get("http://httpbin.org/ip", proxies=proxies)
print(response.text)
```

### 作为库集成到项目中使用

IPClick 可以作为库集成到您的项目中，无需启动独立的服务。这种方式更加灵活，可以根据您的需求自定义配置和插件。

#### 1. 安装 IPClick

```bash
# 安装基础版本
pip install ipclick

# 或者安装完整版本（包含所有可选依赖）
pip install ipclick[all]
```

#### 2. 在项目中初始化 IPClick

```python
import asyncio
import logging
from pathlib import Path

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def main():
    # 导入IPClick
    from ipclick.config import Config
    from ipclick.core.storage_manager import storage_manager

    # 检查配置文件是否存在
    config_file = Path.cwd() / "ipclick.yaml"
    if not config_file.exists():
        logger.info("配置文件不存在，将创建默认配置文件")

        # 初始化配置
        config = Config(str(config_file))

        # 自定义配置（可选）
        config.set('storage.type', 'sqlite')
        config.set('storage.sqlite.path', 'data/my_proxies.db')

        # 保存配置
        config.save()
        logger.info(f"已创建配置文件: {config_file}")
    else:
        logger.info(f"使用现有配置文件: {config_file}")
        config = Config(str(config_file))

    # 获取存储实例
    storage = storage_manager.get_storage()
    logger.info(f"使用存储类型: {storage.__class__.__name__}")

    # 获取代理数量
    count = await storage.count()
    logger.info(f"代理数量: {count}")

    # 如果没有代理，启动调度器获取代理
    if count == 0:
        logger.info("没有代理，将启动调度器获取代理")

        # 导入调度器
        from ipclick.core.scheduler import Scheduler

        # 创建调度器
        scheduler = Scheduler(storage)

        # 运行调度器一次
        logger.info("运行调度器...")
        await scheduler.fetch_proxies()
        await scheduler.validate_proxies()

        # 获取代理数量
        count = await storage.count()
        logger.info(f"代理数量: {count}")

    # 获取随机代理
    proxy = await storage.get_random()
    if proxy:
        logger.info(f"获取到代理: {proxy.host}:{proxy.port} ({proxy.protocol})")

        # 使用代理发送请求
        import httpx

        # 构建代理URL
        proxy_url = proxy.url

        # 发送请求
        try:
            async with httpx.AsyncClient(proxies={
                'http://': proxy_url,
                'https://': proxy_url
            }, timeout=10.0) as client:
                response = await client.get('http://httpbin.org/ip')
                data = response.json()
                logger.info(f"请求成功，IP: {data.get('origin')}")
        except Exception as e:
            logger.error(f"请求失败: {e}")
    else:
        logger.warning("没有可用的代理")

    # 关闭存储
    storage_manager.close_all()

# 运行主函数
asyncio.run(main())
```

#### 3. 创建自定义插件

##### 自定义代理获取器

创建 `custom_plugins/fetchers/my_fetcher.py` 文件：

```python
from ipclick.core.fetcher import BaseFetcher
from ipclick.core.models import Proxy
import logging
import re
from typing import List

logger = logging.getLogger(__name__)

class MyFetcher(BaseFetcher):
    """自定义代理获取器 - 从指定URL获取代理"""

    def __init__(self):
        super().__init__()
        self.name = "my_fetcher"
        self.urls = [
            "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
        ]

    async def fetch(self) -> List[Proxy]:
        """获取代理"""
        logger.info(f"[{self.name}] 开始获取代理...")

        proxies = []

        for url in self.urls:
            try:
                # 获取页面内容
                html = await self.fetch_url(url)
                if not html:
                    continue

                # 解析代理
                pattern = r'(\d+\.\d+\.\d+\.\d+):(\d+)'
                matches = re.findall(pattern, html)

                for match in matches:
                    host, port = match
                    try:
                        port = int(port)
                        proxy = Proxy(
                            host=host,
                            port=port,
                            protocol="http",
                            source=self.name
                        )
                        proxies.append(proxy)
                    except Exception as e:
                        logger.error(f"[{self.name}] 创建代理对象失败: {e}")

                logger.info(f"[{self.name}] 从 {url} 获取到 {len(proxies)} 个代理")
            except Exception as e:
                logger.error(f"[{self.name}] 从 {url} 获取代理失败: {e}")

        return proxies
```

##### 自定义存储后端

创建 `custom_plugins/storages/my_storage.py` 文件：

```python
from ipclick.core.storage import BaseStorage
from ipclick.core.models import Proxy
from ipclick.config import Config
import json
import os
import random
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

class MyStorage(BaseStorage):
    """自定义存储后端 - 使用JSON文件存储代理"""

    def __init__(self):
        self.config = Config()
        self.file_path = self.config.get('storage.my_storage.path', 'data/my_proxies.json')
        self.proxies: Dict[str, Proxy] = {}
        self.max_proxies = self.config.get('storage.max_proxies', 500)

        # 加载数据
        self._load_from_file()

    def _load_from_file(self):
        """从文件加载代理"""
        try:
            if os.path.exists(self.file_path):
                with open(self.file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for proxy_data in data:
                        try:
                            proxy = Proxy.from_dict(proxy_data)
                            self.proxies[proxy.string] = proxy
                        except Exception as e:
                            logger.error(f"加载代理数据失败: {e}")
        except Exception as e:
            logger.error(f"从文件加载代理失败: {e}")

    def _save_to_file(self):
        """保存代理到文件"""
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.file_path)), exist_ok=True)
            with open(self.file_path, 'w', encoding='utf-8') as f:
                data = [proxy.to_dict() for proxy in self.proxies.values()]
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存代理到文件失败: {e}")

    async def add(self, proxy: Proxy) -> bool:
        """添加代理"""
        self.proxies[proxy.string] = proxy
        self._save_to_file()
        return True

    async def update(self, proxy: Proxy) -> bool:
        """更新代理"""
        if proxy.string in self.proxies:
            self.proxies[proxy.string] = proxy
            self._save_to_file()
            return True
        return False

    async def delete(self, proxy: Proxy) -> bool:
        """删除代理"""
        if proxy.string in self.proxies:
            del self.proxies[proxy.string]
            self._save_to_file()
            return True
        return False

    async def get_all(self) -> List[Proxy]:
        """获取所有代理"""
        return list(self.proxies.values())

    async def get_random(self, protocol: Optional[str] = None) -> Optional[Proxy]:
        """获取随机代理"""
        proxies = list(self.proxies.values())
        if protocol:
            proxies = [p for p in proxies if p.protocol == protocol]
        if not proxies:
            return None
        return random.choice(proxies)

    async def count(self, protocol: Optional[str] = None) -> int:
        """获取代理数量"""
        if not protocol:
            return len(self.proxies)
        return len([p for p in self.proxies.values() if p.protocol == protocol])

    async def clear(self) -> bool:
        """清空所有代理"""
        self.proxies.clear()
        self._save_to_file()
        return True
```

#### 4. 配置自定义插件

在 `ipclick.yaml` 文件中配置自定义插件：

```yaml
# 存储配置
storage:
  type: my_storage  # 使用自定义存储
  my_storage:
    path: data/my_proxies.json
  max_proxies: 1000

# 插件配置
plugins:
  fetchers_dir: custom_plugins/fetchers  # 自定义代理获取器目录
  storages_dir: custom_plugins/storages  # 自定义存储后端目录
  enabled: true
  auto_discover: true
```

### 更多示例

更多示例请查看 `examples/user_project_example` 目录，其中包含：

- `simple_example.py`: 简单使用示例
- `advanced_example.py`: 高级使用示例，包括过滤代理、批量请求等
- `web_scraper.py`: 网页爬虫示例，展示如何在爬虫中使用代理池
- `custom_plugins/`: 自定义插件示例

## 在自己的项目中使用 IPClick

IPClick 设计为可以轻松集成到您自己的项目中。以下是在您自己的项目中使用 IPClick 的步骤：

### 1. 安装 IPClick

```bash
# 安装基础版本
pip install ipclick
```

### 2. 初始化配置文件

在您的项目目录中运行以下命令，生成配置文件：

```bash
# 切换到您的项目目录
cd your_project_dir

# 初始化配置文件
ipclick init
```

这将在您的项目目录中创建一个 `ipclick.yaml` 配置文件。

### 3. 自定义配置

根据您的需求编辑 `ipclick.yaml` 文件：

```yaml
# 存储配置
storage:
  type: sqlite  # 可选: memory, sqlite, redis, mysql, mongodb
  max_proxies: 1000

  # SQLite 存储配置
  sqlite:
    path: data/proxy_pool.db  # 相对于您项目目录的路径
    table: proxies

# 插件配置
plugins:
  fetchers_dir: custom_plugins/fetchers  # 自定义代理获取器目录
  storages_dir: custom_plugins/storages  # 自定义存储后端目录
  enabled: true
  auto_discover: true
```

### 4. 创建自定义插件（可选）

如果您需要自定义代理获取器或存储后端，可以创建自定义插件：

```bash
# 创建插件目录
mkdir -p custom_plugins/fetchers
mkdir -p custom_plugins/storages

# 创建自定义代理获取器
touch custom_plugins/fetchers/my_fetcher.py

# 创建自定义存储后端
touch custom_plugins/storages/my_storage.py
```

### 5. 在代码中使用 IPClick

```python
import asyncio
import logging
from pathlib import Path

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def main():
    # 导入IPClick
    from ipclick.config import Config
    from ipclick.core.storage_manager import storage_manager

    # 获取存储实例
    storage = storage_manager.get_storage()
    logger.info(f"使用存储类型: {storage.__class__.__name__}")

    # 获取随机代理
    proxy = await storage.get_random()
    if proxy:
        logger.info(f"获取到代理: {proxy.host}:{proxy.port} ({proxy.protocol})")

        # 使用代理发送请求
        import httpx
        proxy_url = proxy.url
        async with httpx.AsyncClient(proxies={
            'http://': proxy_url,
            'https://': proxy_url
        }) as client:
            response = await client.get('http://httpbin.org/ip')
            data = response.json()
            logger.info(f"请求成功，IP: {data.get('origin')}")

    # 关闭存储
    storage_manager.close_all()

# 运行主函数
asyncio.run(main())
```

这样，您就可以在自己的项目中使用 IPClick 提供的代理池功能，而不需要启动独立的服务。

## 更多示例

IPClick 提供了多个示例文件，展示了如何在不同场景下使用代理池：

- **simple_client.py**: 简单的客户端示例，展示了如何使用 IPClick 作为库来获取和使用代理
- **client_example.py**: 更完整的客户端示例，展示了如何通过 HTTP 和 SOCKS 方式使用代理
- **high_concurrency_example.py**: 高并发环境下使用 IPClick 代理池的示例，展示了如何避免多个爬虫使用同一个代理 IP

这些示例文件位于项目根目录的 `examples/` 目录中，每个示例都有详细的注释和使用说明。

有关示例的更多信息，请参考 `examples/README.md` 文件。

## 发布包

IPClick 提供了一个发布脚本，用于构建和发布包到 PyPI：

```bash
# 运行完整的发布流程（测试、构建、检查和上传）
python publish.py

# 跳过测试
python publish.py --no-test

# 只构建和检查，不上传到 PyPI
python publish.py --no-upload
```

发布脚本会自动清理构建文件、运行测试、构建包、检查包和上传包到 PyPI。

## 贡献

欢迎贡献代码、报告问题或提出建议。请通过 GitHub Issues 或 Pull Requests 参与项目开发。

## 许可证

本项目采用 MIT 许可证。详见 [LICENSE](LICENSE) 文件。
