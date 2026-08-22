"""统一配置控制台与滚动文件日志输出。"""

import contextlib
from pathlib import Path
import sys
import threading
from typing import Any, ClassVar

from loguru import logger

from ipclick.utils.path_util import PathUtil


def _validate_format(value: object) -> str | None:
    """校验用户日志格式是否使用 loguru 语法且保留正文。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if "%(" in text:
        logger.warning(
            "[LOG].format 用的是标准库 logging 的 %(...)s 写法，本项目底层是 loguru，"
            "占位符应写成 {time}/{level}/{message}。已忽略该配置，改用内置格式"
        )
        return None
    if "{message}" not in text:
        logger.warning("[LOG].format 里没有 {message}，日志正文会丢失。已忽略该配置，改用内置格式")
        return None
    return text


_LEVEL_ALIASES = {"WARN": "WARNING", "FATAL": "CRITICAL", "ERR": "ERROR", "TRACE": "TRACE"}


class LogUtil:
    """惰性初始化并代理全局 loguru logger 的兼容门面。"""

    _configurations: ClassVar[dict[str, dict[str, Any]]] = {}
    _default_logger_name: ClassVar[str] = "default"
    _depth: ClassVar[int] = 1
    _emitter: ClassVar[Any] = None
    _own_handler_ids: ClassVar[set[int]] = set()
    _dropped_default_handler: ClassVar[bool] = False
    _configuration_lock: ClassVar[threading.RLock] = threading.RLock()

    def __init__(self, logger_name: str | None = None) -> None:
        """创建日志门面；名称仅保留给旧调用方，消息仍发往全局 handler。"""
        self._logger_name: str = logger_name or self._default_logger_name

    @classmethod
    def init(
        cls,
        level: str = "INFO",
        *,
        logger_name: str = "default",
        format: str | None = None,
        log_file: str | Path | None = None,
        base_dir: Path | None = None,
        rotation: str = "10 MB",
        retention: str | int = "30 days",
        **kwargs: Any,
    ) -> None:
        """安装控制台、可选滚动文件及数据库 handler。"""
        with cls._configuration_lock:
            cls._init_handlers(
                level=level,
                logger_name=logger_name,
                format=format,
                log_file=log_file,
                base_dir=base_dir,
                rotation=rotation,
                retention=retention,
                kwargs=kwargs,
            )

    @classmethod
    def _init_handlers(
        cls,
        *,
        level: str,
        logger_name: str,
        format: str | None,
        log_file: str | Path | None,
        base_dir: Path | None,
        rotation: str,
        retention: str | int,
        kwargs: dict[str, Any],
    ) -> None:
        """在持有配置锁时替换指定名称的 handler。"""
        console_format = (
            "[<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green>]"
            "<level> {level: <9}</level>"
            "[<cyan>{process.name}:{process}</cyan>]"
            "[<magenta>{thread.name}:{thread}</magenta>]"
            " <bold>[<yellow>{file}</yellow>]<yellow>{name}</yellow>:<yellow>{function}</yellow>:<underline>{line}</underline></bold> "
            "| <level>{message}</level>"
        )
        level = level.upper()
        level = _LEVEL_ALIASES.get(level, level)

        cls._drop_loguru_default_handler()

        if logger_name in cls._configurations:
            # 重复初始化同名 logger 时先拆掉旧 handler，避免每条日志重复输出。
            cls._remove_handlers(cls._configurations[logger_name]["handler_ids"])

        handler_ids: list[int] = []

        console_handler = logger.add(
            sys.stderr,
            level=level,
            colorize=True,
            format=format or console_format,
            **kwargs,
        )
        handler_ids.append(console_handler)

        if log_file:
            resolved_path = PathUtil.resolve_log_file(log_file, base_dir)

            PathUtil.ensure_parent_dir(resolved_path)
            file_handler = logger.add(
                str(resolved_path),
                level=level,
                colorize=False,
                format=format or console_format,
                rotation=rotation,
                retention=retention,
                compression="gz",
                **kwargs,
            )
            handler_ids.append(file_handler)

        cls._emitter = None

        cls._own_handler_ids.update(handler_ids)
        cls._configurations[logger_name] = {
            "handler_ids": handler_ids,
            "level": level,
        }

    @classmethod
    def _drop_loguru_default_handler(cls) -> None:
        """只移除一次 loguru 自带的 stderr handler。"""
        if cls._dropped_default_handler:
            return
        cls._dropped_default_handler = True
        with contextlib.suppress(ValueError):
            logger.remove(0)

    @classmethod
    def _remove_handlers(cls, handler_ids: list[int]) -> None:
        """容错移除一组由本类安装的 handler。"""
        for handler_id in handler_ids:
            with contextlib.suppress(ValueError):
                logger.remove(handler_id)
            cls._own_handler_ids.discard(handler_id)

    @classmethod
    def init_from_config(
        cls,
        log_config: dict[str, Any] | None,
        *,
        logger_name: str = "default",
        debug: bool = False,
    ) -> None:
        """从 ``[LOG]`` 配置初始化 logger。"""
        from ipclick.utils.coerce import as_int

        config = dict(log_config or {})
        output = str(config.get("output", "stdout"))
        rotation_config = dict(config.get("rotation", {}))
        # 都要过 as_int（告警后回落默认值），别再裸 int()/裸插值。这两项原来直接
        # int() 和 f"{...} MB"：max_backups = "30 days"（loguru 自己认的保留期写法）
        # 或 max_size = "100MB" 都会抛 ValueError——而调用点一个在 IPClickServer.__init__
        # （启动直接死，报错里不提是哪个键），一个在 Web 端改日志级别时（500）。
        # 日志配置写错不该让服务起不来。
        max_size = as_int(rotation_config.get("max_size"), 100, minimum=1)
        max_backups = as_int(rotation_config.get("max_backups"), 5, minimum=0)

        cls.init(
            level="DEBUG" if debug else str(config.get("level", "INFO")),
            logger_name=logger_name,
            format=_validate_format(config.get("format")),
            log_file=None if output in ("stdout", "stderr", "") else output,
            rotation=f"{max_size} MB",
            retention=max_backups,
        )

    @classmethod
    def remove_logger(cls, logger_name: str) -> None:
        """移除指定配置对应的全部 handler。"""
        with cls._configuration_lock:
            if logger_name in cls._configurations:
                cls._remove_handlers(cls._configurations[logger_name]["handler_ids"])
                del cls._configurations[logger_name]
                cls._emitter = None

    @classmethod
    def _emit(cls) -> Any:
        """返回带正确调用栈深度的缓存发射器。"""
        emitter = cls._emitter
        if emitter is None:
            cls._ensure_configured()
            emitter = cls._emitter = logger.opt(depth=cls._depth)
        return emitter

    @classmethod
    def _ensure_configured(cls, logger_name: str = "default") -> None:
        """确保指定名称至少安装了默认日志配置。"""
        with cls._configuration_lock:
            if logger_name not in cls._configurations:
                cls.init(logger_name=logger_name)

    @classmethod
    def trace(cls, message: str, *args: Any, **kwargs: Any) -> None:
        """记录 TRACE 消息。"""
        cls._emit().trace(message, *args, **kwargs)

    @classmethod
    def debug(cls, message: str, *args: Any, **kwargs: Any) -> None:
        """记录 DEBUG 消息。"""
        cls._emit().debug(message, *args, **kwargs)

    @classmethod
    def info(cls, message: str, *args: Any, **kwargs: Any) -> None:
        """记录 INFO 消息。"""
        cls._emit().info(message, *args, **kwargs)

    @classmethod
    def success(cls, message: str, *args: Any, **kwargs: Any) -> None:
        """记录 SUCCESS 消息。"""
        cls._emit().success(message, *args, **kwargs)

    @classmethod
    def warning(cls, message: str, *args: Any, **kwargs: Any) -> None:
        """记录 WARNING 消息。"""
        cls._emit().warning(message, *args, **kwargs)

    @classmethod
    def error(cls, message: str, *args: Any, **kwargs: Any) -> None:
        """记录 ERROR 消息。"""
        cls._emit().error(message, *args, **kwargs)

    @classmethod
    def critical(cls, message: str, *args: Any, **kwargs: Any) -> None:
        """记录 CRITICAL 消息。"""
        cls._emit().critical(message, *args, **kwargs)

    @classmethod
    def exception(cls, message: str, *args: Any, **kwargs: Any) -> None:
        """记录当前异常堆栈。"""
        cls._emit().exception(message, *args, **kwargs)


log = LogUtil()
