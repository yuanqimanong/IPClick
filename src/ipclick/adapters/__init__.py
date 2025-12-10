# -*- coding:utf-8 -*-

"""
适配器模块 - 统一的HTTP下载器接口

@time: 2025-12-10
@author: Hades
@file: __init__.py
"""

from typing import Dict, Type

from ipclick import Downloader
from ipclick.dto import Response

# 适配器注册表
_ADAPTERS: Dict[str, Type[Downloader]] = {}


def register_adapter(name: str, adapter_class: Type[Downloader]):
    """注册适配器"""
    _ADAPTERS[name] = adapter_class


def get_adapter(name: str) -> Type[Downloader]:
    """获取适配器类"""
    if name not in _ADAPTERS:
        raise ValueError(f"Unknown adapter: {name}. Available:  {list(_ADAPTERS.keys())}")
    return _ADAPTERS[name]


def list_adapters() -> Dict[str, Type[Downloader]]:
    """列出所有可用的适配器"""
    return _ADAPTERS.copy()


def create_adapter(name: str, **kwargs) -> Downloader:
    """创建适配器实例"""
    adapter_class = get_adapter(name)
    return adapter_class(**kwargs)


def is_adapter_available(name: str) -> bool:
    """检查适配器是否可用"""
    return name in _ADAPTERS


# 自动注册可用的适配器
def _register_available_adapters():
    """自动注册可用的适配器"""

    # 尝试注册curl_cffi适配器（优先级最高）
    try:
        from .curl_cffi_adapter import CurlCffiAdapter, is_available as curl_cffi_available
        if curl_cffi_available():
            register_adapter('curl_cffi', CurlCffiAdapter)
            register_adapter('curlcffi', CurlCffiAdapter)  # 别名
            register_adapter('default', CurlCffiAdapter)  # 默认别名
    except ImportError:
        pass

    # 尝试注册httpx适配器
    try:
        from .httpx_adapter import HttpxAdapter, is_available as httpx_available
        if httpx_available():
            register_adapter('httpx', HttpxAdapter)
            # 如果curl_cffi不可用，httpx作为默认
            if 'default' not in _ADAPTERS:
                register_adapter('default', HttpxAdapter)
    except ImportError:
        pass


# 初始化时注册适配器
_register_available_adapters()


def get_default_adapter() -> str:
    """获取默认适配器名称"""
    # 优先级：curl_cffi > httpx
    if 'curl_cffi' in _ADAPTERS:
        return 'curl_cffi'
    elif 'httpx' in _ADAPTERS:
        return 'httpx'
    else:
        raise RuntimeError("No HTTP adapters available.  Please install curl-cffi or httpx.")


def get_adapter_info() -> Dict[str, Dict[str, any]]:
    """获取适配器信息"""
    info = {}
    for name, adapter_class in _ADAPTERS.items():
        info[name] = {
            'class': adapter_class.__name__,
            'module': adapter_class.__module__,
            'available': True
        }
    return info


__all__ = [
    'Downloader',
    'Response',
    'register_adapter',
    'get_adapter',
    'list_adapters',
    'create_adapter',
    'is_adapter_available',
    'get_default_adapter',
    'get_adapter_info'
]
