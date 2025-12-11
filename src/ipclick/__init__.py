# -*- coding:utf-8 -*-

"""
@time: 2025-12-08
@author: Hades
@file: __init__.py
"""

from ipclick.config_loader import load_config
from ipclick.dto.models import DownloadTask, DownloadResponse, HttpMethod, Adapter, ProxyConfig
from ipclick.sdk import Downloader, downloader

__version__ = "1.0.0"
__author__ = "Hades"

__all__ = [
    'Downloader',
    'downloader',
    'DownloadTask',
    'DownloadResponse',
    'HttpMethod',
    'Adapter',
    'ProxyConfig',
    'load_config'
]
