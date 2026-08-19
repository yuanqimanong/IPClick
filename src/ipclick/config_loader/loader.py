from functools import lru_cache
import os
from pathlib import Path
from typing import Any

from ipclick.config_loader.dotenv import load_dotenv
from ipclick.utils.config_util import ConfigUtil, Settings


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
    for name, (section, key, caster) in ENV_OVERRIDES.items():
        raw = os.getenv(name)
        if raw is None or raw == "":
            continue
        try:
            value = caster(raw)
        except (TypeError, ValueError):
            print(f"[ipclick] 忽略非法的环境变量 {name}={raw!r}（期望 {caster.__name__}）")
            continue
        config.setdefault(section, {})[key] = value


def candidate_names(port: int | None = None) -> list[str]:
    names: list[str] = []
    if port:
        names += [f"ipclick-{port}.toml", f".ipclick-{port}.toml"]
    return [*names, "ipclick.toml", ".ipclick.toml"]


@lru_cache(maxsize=8)
def load_config(config_path: str | Path | None = None, port: int | None = None) -> Settings:
    load_dotenv()

    config_list: list[Any] = [DEFAULT_CONFIG_PATH, HOME_CONFIG_PATH]

    if config_path:
        user_path = Path(config_path)
    else:
        for name in candidate_names(port):
            if Path(name).exists():
                user_path = Path(name)
                break
        else:
            user_path = None

    if user_path and user_path.exists():
        config_list.append(user_path)

    config = ConfigUtil.load(config_list)
    _apply_env_overrides(config)
    return config


def example_config() -> str:
    return DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")


def example_env() -> str:
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
