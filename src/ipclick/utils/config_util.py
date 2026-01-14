import tomllib
from pathlib import Path

from box import Box as Settings

from ipclick.utils.log_util import log

log.init("debug")


class ConfigError(tomllib.TOMLDecodeError):
    """配置错误基类"""

    pass


class SettingConvertError(Exception):
    """配置文件转换错误"""

    pass


class ConfigUtil:
    @staticmethod
    def load(path: str | Path | list[str | Path], encoding: str = "utf-8") -> Settings:
        log.debug(f"path ==> {repr(path)}")
        file_paths = [path] if isinstance(path, (str, Path)) else path

        setting_config_list: list[Settings] = []
        for file_path in file_paths:
            path = Path(file_path)
            if not path.exists():
                log.warning(f"配置文件 {file_path} 不存在！")
                continue
            try:
                with open(path, "r", encoding=encoding) as f:
                    config = tomllib.loads(f.read())
                    try:
                        setting = Settings(config)
                        setting_config_list.append(setting)
                    except SettingConvertError:
                        log.exception(f"配置文件 {file_path} 转换 Settings出错！")

            except tomllib.TOMLDecodeError:
                log.exception(f"配置文件 {file_path} 解析出错！")

        return ConfigUtil.merge(setting_config_list)

    @staticmethod
    def merge(settings: list[Settings]) -> Settings:
        setting_merged: Settings = settings[0].copy()

        for setting in settings[1:]:
            setting_merged += setting

        return setting_merged
