import logging
import tomllib
from pathlib import Path
from typing import Any

from ipclick.utils.log_util import LogMixin


class ConfigBase: ...


class ConfigLoader(ConfigBase, LogMixin):
    """
    配置文件加载器

    config_path: 配置文件路径，当传入list时，会按顺序尝试加载多个配置文件，相同字段后面覆盖前面的值。
    """

    def __init__(self, config_path: str | Path | list[str | Path]) -> None:
        config_path_list: list[Path] = []
        if isinstance(config_path, list):
            config_path_list = [Path(p) for p in config_path]
        elif config_path:
            config_path_list = [Path(config_path)]

        self.config_path: list[Path] = config_path_list

    # def load(self) -> dict[str, Any]:
    #     """收集所有配置源并合并"""
    #     config_data = {}
    #
    #     # 按优先级从低到高加载并合并配置
    #     for path in self.config_path:
    #         loaded_config = self._load_toml_file(path)
    #         if loaded_config:
    #             config_data = self._deep_merge_dicts(config_data, loaded_config)
    #
    #     # 3. 应用环境变量（最高优先级）
    #     config_data = self._apply_env_vars(config_data)
    #
    #     return config_data
    #
    # def _load_toml_file(self, filepath: Path):
    #     try:
    #         if filepath.exists():
    #             with open(filepath, "rb") as f:
    #                 config = tomllib.load(f)
    #                 logging.debug(f"加载配置文件: {filepath}")
    #                 return config
    #     except Exception as e:
    #         logging.warning(f"加载配置文件 {filepath} 失败: {e}")
    #     return None


if __name__ == "__main__":
    c = ConfigLoader(r"D:\Projects\IPClick\src\ipclick\configs\default_config.toml")
    try:
        a=0
        b=0
        c = a/b
    except Exception as e:
        c.logger.exception(str(e),from_decorator=True)
