# -*- coding:utf-8 -*-

"""
@time: 2025-12-08
@author: Hades
@file: __init__.py
"""

from .config_loader import load_config
from .dto.models import DownloadTask, DownloadResponse, HttpMethod, Adapter, ProxyConfig
from .sdk import Downloader, downloader, get, post, download

__version__ = "1.0.0"
__author__ = "Hades"

__all__ = [
    'Downloader',
    'downloader',
    'get',
    'post',
    'download',
    'DownloadTask',
    'DownloadResponse',
    'HttpMethod',
    'Adapter',
    'ProxyConfig',
    'load_config'
]
