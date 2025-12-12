# -*- coding:utf-8 -*-

"""
Config 加载器
加载顺序：指定路径配置 > Home目录配置 > 包内默认配置
环境变量：覆盖服务端 IP 和 PORT 配置

@time: 2025-12-10
@author: Hades
@file: loader.py
"""

import os
import tomllib
from functools import lru_cache
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "configs" / "default_config.toml"


@lru_cache(maxsize=3)
def load_config(config_path: str | Path | None = None):
    config = {}

    # 1. 包内默认配置文件（可选，如果你有的话）
    if DEFAULT_CONFIG_PATH.exists():
        with open(DEFAULT_CONFIG_PATH, "rb") as f:
            config.update(tomllib.load(f) or {})

    # 2. 用户家目录配置（~/.ipclick/config.toml）
    home_config = Path.home() / ".ipclick" / "config.toml"
    if home_config.exists():
        with open(home_config, "rb") as f:
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
        with open(user_path, "rb") as f:
            user_config = tomllib.load(f) or {}
            config.update(user_config)

    # 4. 环境变量覆盖（最高优先级）
    if os.getenv("IPCLICK_HOST"):
        config.setdefault("SERVER", {})["host"] = os.getenv("IPCLICK_HOST")
    if os.getenv("IPCLICK_PORT"):
        config.setdefault("SERVER", {})["port"] = int(os.getenv("IPCLICK_PORT"))

    return config
