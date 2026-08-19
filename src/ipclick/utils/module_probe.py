"""不执行可选模块代码地探测其安装状态和发行包版本。"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import threading


_installed_cache: dict[str, bool] = {}
_version_cache: dict[str, str | None] = {}
_lock = threading.Lock()


def installed(module: str) -> bool:
    """线程安全地缓存并返回模块是否可被导入系统发现。"""
    cached = _installed_cache.get(module)
    if cached is not None:
        return cached

    with _lock:
        if module in _installed_cache:
            return _installed_cache[module]
        _installed_cache[module] = _find(module)
        return _installed_cache[module]


def _find(module: str) -> bool:
    """执行一次不导入模块本体的 ``find_spec`` 探测。"""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, AttributeError, ValueError):
        return False


def version(distribution: str) -> str | None:
    """返回发行包版本；包不存在或元数据损坏时返回 ``None``。"""
    if distribution in _version_cache:
        return _version_cache[distribution]
    with _lock:
        try:
            resolved: str | None = importlib.metadata.version(distribution)
        except Exception:
            resolved = None
        _version_cache[distribution] = resolved
        return resolved


def invalidate() -> None:
    """清空导入系统及本模块缓存，供组件安装或卸载后刷新。"""
    importlib.invalidate_caches()
    with _lock:
        _installed_cache.clear()
        _version_cache.clear()


__all__ = ["installed", "invalidate", "version"]
