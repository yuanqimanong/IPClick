"""
Config 加载器

配置文件加载顺序 (1>2>3)：
    1. 指定配置文件路径
    2. Home目录配置
    3. 包内默认配置
环境变量：覆盖服务端 IP 和 PORT 配置
"""

import os
import time
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional, TypedDict

from pydantic import BaseModel, Field, field_validator


class GeneralConfig(BaseModel):
    mode: str = "standalone"
    env: str = "dev"
    debug: bool = False


class ProxyConfig(BaseModel):
    scheme: str = ""
    host: str = ""
    port: int = 9527
    auth_key: str = ""
    auth_password: str = ""
    channel_name: str = ""
    session_ttl: int = 60
    country_code: str = ""
    tunnel_server: str = ""

    @field_validator("port")
    @classmethod
    def validate_port(cls, v: int) -> int:
        if v < 1 or v > 65535:
            raise ValueError("端口必须在 1-65535 范围内")
        return v


class ClusterNodeConfig(BaseModel):
    id: str = ""
    address: str = ""
    region: str = ""
    zone: str = ""
    weight: int = 100


class ClusterConfig(BaseModel):
    load_balancer: str = ""
    nodes: list[ClusterNodeConfig] = Field(default_factory=list)
    db_uri: str = ""


class ServerConfig(BaseModel):
    host: str = "[::]"
    port: int = 9527
    max_workers: int = 100


class AppConfig(BaseModel):
    GENERAL: GeneralConfig
    PROXY: ProxyConfig
    CLUSTER: ClusterConfig
    SERVER: ServerConfig


DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "configs" / "default_config.toml"


@lru_cache(maxsize=3)
def load_config(config_path: str | Path | None = None) -> AppConfig:
    config = {}

    # 1. 包内默认配置文件（可选，如果你有的话）
    if DEFAULT_CONFIG_PATH.exists():
        with open(DEFAULT_CONFIG_PATH, "rb") as f:
            toml_data = tomllib.load(f)
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
        config.setdefault("SERVER", {})["port"] = int(os.getenv("IPCLICK_PORT", 9527))

    return config
