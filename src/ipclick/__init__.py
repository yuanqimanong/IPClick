# -*- coding:utf-8 -*-

import importlib.metadata


try:
    __version__ = importlib.metadata.version("ipclick")
    __author__ = importlib.metadata.metadata("ipclick")["Author"]
except importlib.metadata.PackageNotFoundError:
    __version__ = "1.0.0"
    __author__ = "Hades"

# # 延迟导入或者重新组织导入结构
# from ipclick.config_loader import load_config
#
# # 只导入DTO模型，不从sdk导入Downloader
# from ipclick.dto.models import (
#     Adapter,
#     DownloadResponse,
#     DownloadTask,
#     HttpMethod,
#     ProxyConfig,
# )
#
# # 暂时保留Downloader导入，但建议检查sdk.py是否有反向导入
# from ipclick.sdk import Downloader
#
__all__ = [
    "downloader",
    "DownloadTask",
    "DownloadResponse",
    "HttpMethod",
    "Adapter",
    "ProxyConfig",
    "load_config",
]
