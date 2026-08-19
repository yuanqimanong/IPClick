"""加载、合并 TOML 文件，并将嵌套配置包装为可访问对象。"""

from collections.abc import Mapping
from pathlib import Path
import tomllib
from typing import Any, cast

from box import Box

from ipclick.exceptions import ConfigError
from ipclick.utils.log_util import log


class Settings(Box):
    """IPClick 配置对象，保留 ``Box`` 的映射和属性访问能力。"""


def section(config: Mapping[str, Any] | None, name: str) -> dict[str, Any]:
    """安全提取配置表；缺失或类型错误时返回空字典。"""
    if not config:
        return {}
    value = config.get(name)
    if isinstance(value, Mapping):
        return dict(value)
    if value is not None:
        log.warning(f"配置节 [{name}] 不是一个表（得到 {type(value).__name__}），已按空配置处理")
    return {}


class ConfigUtil:
    """TOML 配置文件的加载与覆盖合并工具。"""

    @staticmethod
    def load(path: str | Path | list[str | Path], encoding: str = "utf-8") -> Settings:
        """依次读取配置文件，并让后加载文件覆盖先加载文件。"""
        log.debug(f"load path ==> {path!r}")
        file_paths = [path] if isinstance(path, (str, Path)) else path

        setting_config_list: list[Settings] = []
        for file_path in file_paths:
            path = Path(file_path)
            if not path.exists():
                log.debug(f"配置文件 {file_path} 不存在")
                continue
            try:
                with open(path, encoding=encoding) as f:
                    config = tomllib.loads(f.read())
                try:
                    setting_config_list.append(Settings(config))
                except (TypeError, ValueError, AttributeError) as e:
                    raise ConfigError(f"配置文件 {file_path} 无法转换为 Settings：{e}") from e
            except tomllib.TOMLDecodeError as e:
                raise ConfigError(f"配置文件 {file_path} 不是合法 TOML：{e}") from e
            except OSError as e:
                raise ConfigError(f"读取配置文件 {file_path} 失败：{e}") from e

        return ConfigUtil.merge(setting_config_list)

    @staticmethod
    def merge(settings: list[Settings]) -> Settings:
        """深度合并配置对象列表，不修改输入对象。"""
        if not settings:
            return Settings()

        setting_merged = cast(Settings, settings[0].copy())
        for setting in settings[1:]:
            setting_merged += setting

        return setting_merged
