"""共享 fixture。

所有测试都不访问真实网络：适配器要么被替换成假实现，要么只测纯函数。
"""

from collections.abc import Iterator
from pathlib import Path
import sys
from typing import Any

import pytest


# 让 tests 目录下的 helper 可以直接 import
sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def _reset_config_cache() -> Iterator[None]:
    """load_config 带 lru_cache，测试之间必须清掉，否则配置串味。"""
    from ipclick.config_loader.loader import load_config

    load_config.cache_clear()
    yield
    load_config.cache_clear()


@pytest.fixture(autouse=True)
def _reset_downloader_cache() -> Iterator[None]:
    """清理全局下载器缓存，避免测试间共享 gRPC channel。

    缓存在 factory 里（不在 sdk 里）：让 sdk 去解释 [GENERAL].mode 会形成
    sdk -> factory -> cluster -> sdk 的导入环。
    """
    from ipclick import factory

    factory._downloader_cache.clear()
    yield
    for instance in factory._downloader_cache.values():
        instance.close()
    factory._downloader_cache.clear()


@pytest.fixture
def tmp_config(tmp_path: Path) -> Any:
    """生成一个临时 TOML 配置文件，返回其路径。"""

    def _make(content: str, name: str = "ipclick.toml") -> Path:
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        return path

    return _make
