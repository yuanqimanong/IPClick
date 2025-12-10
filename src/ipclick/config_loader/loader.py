# -*- coding:utf-8 -*-

"""
@time: 2025-12-10
@author: Hades
@file: loader.py
"""
import os
import tomllib
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(__file__).parent / "configs" / "default_config.toml"
DEFAULT_CONFIG = tomllib.load(DEFAULT_CONFIG_PATH.open("rb", encoding="utf-8")) or {}


def load_config(config_path: str | Path | None = None):
    config = DEFAULT_CONFIG.copy()  # 先加载内置默认

    # 1. 包内默认配置文件（可选，如果你有的话）
    if DEFAULT_CONFIG_PATH.exists():
        with open(DEFAULT_CONFIG_PATH, "rb", encoding="utf-8") as f:
            config.update(tomllib.load(f) or {})

    # 2. 用户家目录配置（~/.ipclick/config.toml）
    home_config = Path.home() / ".ipclick" / "config.toml"
    if home_config.exists():
        with open(home_config, "rb", encoding="utf-8") as f:
            config.update(tomllib.load(f) or {})

    # 3. 当前项目目录配置（最高优先级）
    if config_path:
        user_path = Path(config_path)
    else:
        # 自动查找常见文件名
        for name in ["config.toml", "ipclick.toml", ".ipclick.toml"]:
            if Path(name).exists():
                user_path = Path(name)
                break
        else:
            user_path = None

    if user_path and user_path.exists():
        with open(user_path, "rb", encoding="utf-8") as f:
            user_config = tomllib.load(f) or {}
            config.update(user_config)

    # 4. 环境变量覆盖（最高优先级）
    if os.getenv("IPCLICK_API_KEY"):
        config["api_key"] = os.getenv("IPCLICK_API_KEY")
    # ... 其他字段同理

    return config
