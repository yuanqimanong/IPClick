import importlib.metadata

from ipclick.dto.models import (
    DownloadResponse,
    DownloadTask,
    HttpMethod,
    IPClickAdapter,
    ProxyConfig,
)
from ipclick.dto.response import Response
from ipclick.exceptions import (
    AdapterError,
    ConfigError,
    IPClickError,
    RequestError,
    TransportError,
    URLNotAllowedError,
    ValidationError,
)
from ipclick.sdk import Downloader, close_all_downloaders, downloader, get_downloader


try:
    __version__ = importlib.metadata.version("ipclick")
    __author__ = importlib.metadata.metadata("ipclick")["Author"]
except importlib.metadata.PackageNotFoundError:  # pragma: no cover - 仅在未安装时走到
    __version__ = "0.2.0"
    __author__ = "元气码农少女酱"


__all__ = [  # noqa: RUF022 - 按用途分组比字母序更易读
    # 客户端
    "Downloader",
    "get_downloader",
    "close_all_downloaders",
    "downloader",
    # 数据模型
    "DownloadTask",
    "DownloadResponse",
    "Response",
    "HttpMethod",
    "IPClickAdapter",
    "ProxyConfig",
    # 异常
    "IPClickError",
    "ConfigError",
    "AdapterError",
    "TransportError",
    "RequestError",
    "ValidationError",
    "URLNotAllowedError",
    # 元数据
    "__version__",
    "__author__",
]
