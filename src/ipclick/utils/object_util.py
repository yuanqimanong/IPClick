# from dataclasses import dataclass, field
# from datetime import datetime
# from typing import Any
#
#
# @dataclass
# class DotAccess:
#     """点号访问支持"""
#
#     _data: dict[str, Any] = field(default_factory=dict)
#
#     def __getattr__(self, name: str) -> Any:
#         if name.startswith("_"):
#             return super().__getattribute__(name)
#
#         if name in self._data:
#             value = self._data[name]
#             if isinstance(value, dict):
#                 return DotAccess(value)
#             return value
#         raise AttributeError(f"'{self.__class__.__name__}'没有属性 '{name}'")
#
#     def __setattr__(self, name: str, value: Any) -> None:
#         if name.startswith("_"):
#             super().__setattr__(name, value)
#         else:
#             self._data[name] = value
#
#     def __delattr__(self, name: str) -> None:
#         if name in self._data:
#             del self._data[name]
#         else:
#             super().__delattr__(name)
#
#     def __contains__(self, key: str) -> bool:
#         return key in self._data
#
#     def __getitem__(self, key: str) -> Any:
#         value = self._data[key]
#         if isinstance(value, dict):
#             return DotAccess(value)
#         return value
#
#     def __setitem__(self, key: str, value: Any) -> None:
#         self._data[key] = value
#
#     def __delitem__(self, key: str) -> None:
#         del self._data[key]
#
#     def get(self, key: str, default: Any = None) -> Any:
#         """安全获取值"""
#         try:
#             return self._data.get(key, default)
#         except AttributeError:
#             return default
#
#     def to_dict(self) -> dict[str, Any]:
#         """转换为字典"""
#
#         def _to_dict(obj: Any) -> Any:
#             if isinstance(obj, DotAccess):
#                 return {k: _to_dict(v) for k, v in obj._data.items()}
#             elif isinstance(obj, dict):
#                 return {k: _to_dict(v) for k, v in obj.items()}
#             elif isinstance(obj, list):
#                 return [_to_dict(item) for item in obj]
#             else:
#                 return obj
#
#         return _to_dict(self._data)
#
#
# if __name__ == "__main__":
#     # 初始化带数据的对象
#     data = DotAccess()
#     # 使用字典访问方式
#     print(data.user["info"]["email"])
#
#     # 或者先获取中间对象
#     user_info = data.user  # 这会返回字典
#     nested_obj = DotAccess(user_info["info"])
#     print(nested_obj.email)
#
#
#
#
#     data.user = {"name": "李四", "info": {"email": datetime.now()}}
#
#     # 点号访问
#     print(data.user.name)  # 李四
#     print(data.user["info"]["email"])  # 李四
#     print(data.user.info.email)  # test@example.com
#
#     # 字典访问
#     print(data["user"]["name"])  # 李四
#
#     # 安全获取
#     print(data.get("missing", "default"))  # default
#
#     # 转换回字典
#     regular_dict = data.to_dict()
#     print(regular_dict)
#
#
#
# import os
# import sys
# import tomllib
# from dataclasses import asdict, fields, is_dataclass
# from pathlib import Path
# from typing import Any, Dict, List, Optional, Type, TypeVar, Union
#
# from box import Box, BoxList
#
# from ipclick.utils.log_util import log
#
# log.init("debug")
#
#
# T = TypeVar("T")
#
#
# class ConfigError(Exception):
#     pass
#
#
# class ConfigUtil:
#     """
#     使用 Box 的配置工具
#     Box 特性：点号访问、键/属性混合、to_dict() 递归转换、freeze()、strict 模式等
#     """
#
#     @staticmethod
#     def _boxify(obj: Any, box_kwargs: dict[str, Any] | None = None) -> Any:
#         """将 dict/list 递归封装为 Box/BoxList（若已是 Box 则直接返回）"""
#         if isinstance(obj, Box) or isinstance(obj, BoxList):
#             return obj
#         if isinstance(obj, dict):
#             return Box(obj, **(box_kwargs or {}))
#         if isinstance(obj, list):
#             return BoxList([ConfigUtil._boxify(i, box_kwargs) for i in obj])
#         return obj
#
#     @staticmethod
#     def load_files(
#         file_paths: Union[str, Path, List[Union[str, Path]]],
#         encoding: str = "utf-8",
#         box_kwargs: Optional[Dict[str, Any]] = None,
#     ) -> Box:
#         if isinstance(file_paths, (str, Path)):
#             file_paths = [file_paths]
#
#         merged: Dict[str, Any] = {}
#
#         for file_path in file_paths:
#             path = Path(file_path)
#             if not path.exists():
#                 raise ConfigError(f"配置文件不存在: {path}")
#             try:
#                 with open(path, "rb") as f:
#                     # tomllib.load expects a bytes-buffer in py3.11 if using file
#                     config = tomllib.load(f)
#                     ConfigUtil._merge_dict(merged, config)
#             except Exception as e:
#                 raise ConfigError(f"加载配置文件失败 {path}: {e}")
#
#         return ConfigUtil._boxify(merged, box_kwargs)
#
#     @staticmethod
#     def load_string(content: str, box_kwargs: Optional[Dict[str, Any]] = None) -> Box:
#         try:
#             config = tomllib.loads(content)
#             return ConfigUtil._boxify(config, box_kwargs)
#         except Exception as e:
#             raise ConfigError(f"解析TOML字符串失败: {e}")
#
#     @staticmethod
#     def load_env(
#         env_prefix: str = "",
#         ignore_case: bool = True,
#         box_kwargs: Optional[Dict[str, Any]] = None,
#     ) -> Box:
#         config: Dict[str, Any] = {}
#
#         for key, value in os.environ.items():
#             if env_prefix:
#                 if ignore_case and key.upper().startswith(env_prefix.upper()):
#                     key = key[len(env_prefix) :].lstrip("_")
#                 elif key.startswith(env_prefix):
#                     key = key[len(env_prefix) :].lstrip("_")
#                 else:
#                     continue
#
#             if not key:
#                 continue
#
#             keys = key.split("__")
#             current = config
#
#             for k in keys[:-1]:
#                 k = k.lower() if ignore_case else k
#                 if k not in current or not isinstance(current[k], dict):
#                     current[k] = {}
#                 current = current[k]
#
#             last_key = keys[-1].lower() if ignore_case else keys[-1]
#             current[last_key] = ConfigUtil._parse_value(value)
#
#         return ConfigUtil._boxify(config, box_kwargs)
#
#     @staticmethod
#     def merge(*configs: Box, box_kwargs: Optional[Dict[str, Any]] = None) -> Box:
#         merged: Dict[str, Any] = {}
#         for cfg in configs:
#             # accept Box or dict
#             d = cfg.to_dict() if isinstance(cfg, Box) else dict(cfg)
#             ConfigUtil._merge_dict(merged, d)
#         return ConfigUtil._boxify(merged, box_kwargs)
#
#     @staticmethod
#     def to_dict(config: Box) -> Dict[str, Any]:
#         if isinstance(config, Box):
#             return config.to_dict()
#         # allow plain dicts
#         return dict(config)
#
#     @staticmethod
#     def to_settings(config: Box, settings_class: Type[T]) -> T:
#         config_dict = ConfigUtil.to_dict(config)
#         if is_dataclass(settings_class):
#             field_names = {f.name for f in fields(settings_class)}
#             init_data = {}
#             for name in field_names:
#                 if "." in name:
#                     parts = name.split(".")
#                     v = config_dict
#                     for p in parts:
#                         v = v.get(p) if isinstance(v, dict) else None
#                         if v is None:
#                             break
#                     if v is not None:
#                         init_data[name] = v
#                 else:
#                     if name in config_dict:
#                         init_data[name] = config_dict[name]
#             return settings_class(**init_data)
#         else:
#             instance = settings_class()
#             for k, v in config_dict.items():
#                 if hasattr(instance, k):
#                     setattr(instance, k, v)
#             return instance
#
#     @staticmethod
#     def from_settings(
#         settings: Any, box_kwargs: Optional[Dict[str, Any]] = None
#     ) -> Box:
#         if is_dataclass(settings):
#             data = asdict(settings)
#         elif hasattr(settings, "__dict__"):
#             data = settings.__dict__
#         else:
#             data = {}
#         data = {k: v for k, v in data.items() if not k.startswith("_")}
#         return ConfigUtil._boxify(data, box_kwargs)
#
#     @staticmethod
#     def validate(config: Box, rules: Dict[str, Any], strict: bool = False) -> List[str]:
#         errors: List[str] = []
#         config_dict = ConfigUtil.to_dict(config)
#         for path, validator in rules.items():
#             value = ConfigUtil._get_nested_value(config_dict, path)
#             if value is None:
#                 errors.append(f"字段不存在: {path}")
#                 if strict:
#                     continue
#             if callable(validator):
#                 try:
#                     if not validator(value):
#                         errors.append(f"验证失败: {path} = {value}")
#                 except Exception as e:
#                     errors.append(f"验证异常 {path}: {e}")
#             elif isinstance(validator, type):
#                 if not isinstance(value, validator):
#                     errors.append(
#                         f"类型错误 {path}: 期望 {validator.__name__}, 实际 {type(value).__name__}"
#                     )
#         return errors
#
#     @staticmethod
#     def get(
#         config: Box, key: str, default: Any = None, path_separator: str = "."
#     ) -> Any:
#         try:
#             if path_separator in key:
#                 parts = key.split(path_separator)
#                 cur = config
#                 for p in parts:
#                     if isinstance(cur, Box) and p in cur:
#                         cur = cur[p]
#                     else:
#                         return default
#                 return cur
#             else:
#                 return (
#                     config.get(key, default)
#                     if isinstance(config, Box)
#                     else config.get(key, default)
#                 )
#         except Exception:
#             return default
#
#     @staticmethod
#     def set(
#         config: Box,
#         key: str,
#         value: Any,
#         path_separator: str = ".",
#     ) -> None:
#         if path_separator in key:
#             parts = key.split(path_separator)
#             cur = config
#             for p in parts[:-1]:
#                 if p not in cur or not isinstance(cur[p], dict):
#                     cur[p] = {}
#                 cur = cur[p]
#             cur[parts[-1]] = value
#         else:
#             config[key] = value
#
#     @staticmethod
#     def _merge_dict(
#         base: dict[str, Any], new: dict[str, Any], overwrite: bool = True
#     ) -> None:
#         for key, value in new.items():
#             if (
#                 key in base
#                 and isinstance(base[key], dict)
#                 and isinstance(value, dict)
#                 and overwrite
#             ):
#                 ConfigUtil._merge_dict(base[key], value, overwrite)
#             else:
#                 base[key] = value
#
#     @staticmethod
#     def _parse_value(value: str) -> Any:
#         if not isinstance(value, str):
#             return value
#         s = value.strip()
#         if s.lower() in ("true", "yes", "on", "1"):
#             return True
#         if s.lower() in ("false", "no", "off", "0"):
#             return False
#         try:
#             return int(s)
#         except ValueError:
#             pass
#         try:
#             return float(s)
#         except ValueError:
#             pass
#         if "," in s:
#             parts = [p.strip() for p in s.split(",")]
#             return [ConfigUtil._parse_value(p) for p in parts]
#         return s
#
#     @staticmethod
#     def _get_nested_value(data: dict[str, Any], path: str) -> Any:
#         parts = path.split(".")
#         cur = data
#         for p in parts:
#             if isinstance(cur, dict) and p in cur:
#                 cur = cur[p]
#             else:
#                 return None
#         return cur
