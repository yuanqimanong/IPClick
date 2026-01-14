import tomllib
from pathlib import Path
from typing import cast

from box import Box

from ipclick.utils.log_util import log


class Settings(Box):
    """一个用于保存配置设置的 Box 子类。

    该类提供了一个类似字典的接口，支持点符号访问从 TOML 文件加载的配置数据。
    """

    pass


class ConfigUtil:
    """用于从 TOML 文件加载和合并配置的实用类。

    提供静态方法，从一个或多个文件中加载配置，并将它们合并为单个 Settings 对象。
    """

    @staticmethod
    def load(path: str | Path | list[str | Path], encoding: str = "utf-8") -> Settings:
        """从一个或多个 TOML 文件加载配置到 Settings 对象中。

        支持从单个路径或路径列表加载。如果文件不存在，则记录警告并跳过它。合并所有有效的配置。

        Args:
            path: 单个文件路径（str 或 Path）或文件路径列表。
            encoding: 读取文件时使用的编码（默认："utf-8"）。

        Returns:
            来自所有有效文件的合并 Settings 对象。
        """
        log.debug(f"path ==> {repr(path)}")
        file_paths = [path] if isinstance(path, (str, Path)) else path

        setting_config_list: list[Settings] = []
        for file_path in file_paths:
            path = Path(file_path)
            if not path.exists():
                log.warning(f"配置文件 {file_path} 不存在，请检查路径！")
                continue
            try:
                with open(path, "r", encoding=encoding) as f:
                    config = tomllib.loads(f.read())
                    try:
                        setting = Settings(config)
                        setting_config_list.append(setting)
                    except (TypeError, ValueError, AttributeError):
                        log.exception(f"配置文件 {file_path} 转换 Settings出错！")

            except tomllib.TOMLDecodeError:
                log.exception(f"配置文件 {file_path} 解析出错！")

        return ConfigUtil.merge(setting_config_list)

    @staticmethod
    def merge(settings: list[Settings]) -> Settings:
        """将 Settings 对象列表合并为单个 Settings 对象。

        复制第一个 Settings，并使用 += 运算符合并后续的 Settings。

        Args:
            settings: 要合并的 Settings 对象列表。

        Returns:
            一个新的合并 Settings 对象。
        """
        if not settings:
            return Settings()

        setting_merged = cast(Settings, settings[0].copy())
        for setting in settings[1:]:
            setting_merged += setting

        return setting_merged
