"""
Config 加载器

配置文件加载顺序 (1>2>3)：
    1. 指定的配置文件路径
    2. Home目录配置（~/.ipclick/config.toml）
    3. 包内默认配置（可选，如果你有的话）
环境变量：覆盖服务端 IP 和 PORT 配置
"""

from functools import lru_cache
import os
from pathlib import Path
from typing import Any

from ipclick.utils.config_util import ConfigUtil, Settings


DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "configs" / "default_config.toml"
HOME_CONFIG_PATH = Path.home() / ".ipclick" / "config.toml"


@lru_cache(maxsize=3)
def load_config(config_path: str | Path | None = None) -> Settings:
    config_list: list[Any] = [DEFAULT_CONFIG_PATH, HOME_CONFIG_PATH]

    if config_path:
        user_path = Path(config_path)
    else:
        # 自动查找常见文件名
        for name in ["ipclick.toml", ".ipclick.toml"]:
            if Path(name).exists():
                user_path = Path(name)
                break
        else:
            user_path = None

    if user_path and user_path.exists():
        config_list.append(user_path)

    config = ConfigUtil.load(config_list)

    if os.getenv("IPCLICK_HOST"):
        config["SERVER"]["host"] = os.getenv("IPCLICK_HOST")
    if os.getenv("IPCLICK_PORT"):
        config["SERVER"]["port"] = int(os.getenv("IPCLICK_PORT", 9527))

    return config
