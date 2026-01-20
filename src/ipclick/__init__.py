# -*- coding:utf-8 -*-

import importlib.metadata


try:
    __version__ = importlib.metadata.version("ipclick")
    __author__ = importlib.metadata.metadata("ipclick")["Author"]
except importlib.metadata.PackageNotFoundError:
    __version__ = "1.0.0"
    __author__ = "Hades"

from ipclick.dto.models import ProxyConfig
from ipclick.sdk import Downloader, downloader, get_downloader


__all__ = [
    "Downloader",
    "get_downloader",
    "downloader",
    "ProxyConfig",
]
