import importlib.metadata

from ipclick.dto.models import (
    DownloadResponse,
    DownloadTask,
    HttpMethod,
    IPClickAdapter,
    ProxyConfig,
    ResponseTrace,
)
from ipclick.dto.response import Response
from ipclick.exceptions import (
    AdapterError,
    AuthenticationError,
    ClientClosedError,
    ConfigError,
    IPClickError,
    RequestError,
    TransportError,
    URLNotAllowedError,
    ValidationError,
)
from ipclick.factory import close_all_downloaders, create_client, downloader, get_downloader
from ipclick.limiter import HostLimitTimeout
from ipclick.sdk import Downloader


try:
    __version__ = importlib.metadata.version("ipclick")
    __author__ = importlib.metadata.metadata("ipclick")["Author"]
except importlib.metadata.PackageNotFoundError:
    __version__ = "1.0.0"
    __author__ = "元气码农少女酱"


__all__ = [
    "AdapterError",
    "AuthenticationError",
    "ClientClosedError",
    "ConfigError",
    "DownloadResponse",
    "DownloadTask",
    "Downloader",
    "HostLimitTimeout",
    "HttpMethod",
    "IPClickAdapter",
    "IPClickError",
    "ProxyConfig",
    "RequestError",
    "Response",
    "ResponseTrace",
    "TransportError",
    "URLNotAllowedError",
    "ValidationError",
    "__author__",
    "__version__",
    "close_all_downloaders",
    "create_client",
    "downloader",
    "get_downloader",
]
