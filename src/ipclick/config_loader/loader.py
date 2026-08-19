"""按默认、用户目录和项目文件的优先级合并运行配置。"""

from functools import lru_cache
import os
from pathlib import Path
from typing import Any

from ipclick.config_loader.dotenv import load_dotenv
from ipclick.exceptions import ConfigError
from ipclick.utils.config_util import ConfigUtil, Settings
from ipclick.utils.log_util import log


DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "configs" / "default_config.toml"
HOME_CONFIG_PATH = Path.home() / ".ipclick" / "config.toml"

ENV_OVERRIDES: dict[str, tuple[str, str, type]] = {
    "IPCLICK_HOST": ("SERVER", "host", str),
    "IPCLICK_PORT": ("SERVER", "port", int),
    "IPCLICK_MAX_WORKERS": ("SERVER", "max_workers", int),
    "IPCLICK_MODE": ("GENERAL", "mode", str),
    "IPCLICK_LOG_LEVEL": ("LOG", "level", str),
    "IPCLICK_CLUSTER_SELF_ID": ("CLUSTER", "self_id", str),
}


def _apply_env_overrides(config: Settings) -> None:
    """应用少量允许通过环境变量覆盖的非机密运行参数。"""
    for name, (section, key, caster) in ENV_OVERRIDES.items():
        raw = os.getenv(name)
        if raw is None or raw == "":
            continue
        try:
            value = caster(raw)
        except (TypeError, ValueError):
            # 库层不能写 stdout，否则会破坏 CLI ``--json`` 的单文档契约。
            log.warning(f"忽略非法的环境变量 {name}={raw!r}（期望 {caster.__name__}）")
            continue
        config.setdefault(section, {})[key] = value


def candidate_names(port: int | None = None) -> list[str]:
    """返回当前目录中按优先级查找的配置文件名。"""
    names: list[str] = []
    if port:
        names += [f"ipclick-{port}.toml", f".ipclick-{port}.toml"]
    return [*names, "ipclick.toml", ".ipclick.toml"]


@lru_cache(maxsize=8)
def load_config(config_path: str | Path | None = None, port: int | None = None) -> Settings:
    """加载并缓存合并后的配置；后列文件和环境变量优先。"""
    load_dotenv()

    if not DEFAULT_CONFIG_PATH.is_file():
        raise ConfigError(f"随包配置模板不存在：{DEFAULT_CONFIG_PATH}")

    config_list: list[Any] = [DEFAULT_CONFIG_PATH, HOME_CONFIG_PATH]

    if config_path:
        user_path = Path(config_path).expanduser()
        if not user_path.is_file():
            raise ConfigError(f"配置文件不存在或不是普通文件：{user_path}")
    else:
        for name in candidate_names(port):
            if Path(name).is_file():
                user_path = Path(name)
                break
        else:
            user_path = None

    if user_path is not None:
        config_list.append(user_path)

    config = ConfigUtil.load(config_list)
    _apply_env_overrides(config)
    return config


def example_config() -> str:
    """读取随包分发的 TOML 配置模板。"""
    return DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")


def example_env() -> str:
    """生成仅包含机密项的环境变量模板。"""
    from ipclick.secrets import env_template

    return env_template()


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "ENV_OVERRIDES",
    "HOME_CONFIG_PATH",
    "candidate_names",
    "example_config",
    "example_env",
    "load_config",
]
