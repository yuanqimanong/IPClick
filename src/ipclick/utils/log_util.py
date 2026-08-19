from collections.abc import Callable
import contextlib
import functools
from pathlib import Path
import sqlite3
from sqlite3 import Connection
import sys
from typing import Any, ClassVar, Protocol, TypeVar, cast

from loguru import logger
from typing_extensions import override, runtime_checkable

from ipclick.utils.path_util import PathUtil


F = TypeVar("F", bound=Callable[..., Any])


@runtime_checkable
class DatabaseAdapter(Protocol):
    def write(self, log_message: Any) -> None: ...


class SQLiteAdapter(DatabaseAdapter):
    def __init__(self, db_path: str, table_name: str = "logs"):
        if not table_name.isidentifier():
            raise ValueError(f"非法的表名: {table_name!r}（只允许标识符字符）")

        self.db_path: str = db_path
        self.table_name: str = table_name
        sql_path = PathUtil.resolve_path(db_path)
        PathUtil.ensure_parent_dir(sql_path)
        self.conn: Connection = sqlite3.connect(str(sql_path), check_same_thread=False, timeout=10.0)
        self._create_table()

    def _create_table(self):
        cursor = self.conn.cursor()
        _ = cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.table_name} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                level TEXT,
                message TEXT,
                file TEXT,
                line INTEGER,
                function TEXT,
                process_id INTEGER,
                thread_id INTEGER,
                exception TEXT
            )
        """)
        self.conn.commit()

    @override
    def write(self, log_message: Any) -> None:
        record = log_message.record

        timestamp = record["time"].isoformat()
        level = record["level"].name
        message = record["message"]
        file = record["file"].path
        line = record["line"]
        function = record["function"]
        process_id = record["process"].id
        thread_id = record["thread"].id
        exception = str(record["exception"]) if record["exception"] else None

        with self.conn:
            _ = self.conn.execute(
                f"""
                INSERT INTO {self.table_name} (timestamp, level, message, file, line, function, process_id, thread_id, exception)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    level,
                    message,
                    file,
                    line,
                    function,
                    process_id,
                    thread_id,
                    exception,
                ),
            )

    def close(self):
        self.conn.close()


def ensure_configured(func: F) -> F:

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any):
        if args:
            cls = args[0]
            if hasattr(cls, "_ensure_configured"):
                cls._ensure_configured()
        else:
            pass
        return func(*args, **kwargs)

    return cast(F, wrapper)


def _validate_format(value: object) -> str | None:
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
    _configurations: ClassVar[dict[str, dict[str, Any]]] = {}
    _default_logger_name: ClassVar[str] = "default"
    _depth: ClassVar[int] = 1
    _emitter: ClassVar[Any] = None
    _own_handler_ids: ClassVar[set[int]] = set()
    _dropped_default_handler: ClassVar[bool] = False

    def __init__(self, logger_name: str | None = None):
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
        adapter: DatabaseAdapter | None = None,
        **kwargs: Any,
    ) -> None:
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

        if adapter:
            adapter_handler = logger.add(
                adapter.write,
                level=level,
                enqueue=True,
                **kwargs,
            )
            handler_ids.append(adapter_handler)

        cls._emitter = None

        cls._own_handler_ids.update(handler_ids)
        cls._configurations[logger_name] = {
            "handler_ids": handler_ids,
            "level": level,
            "adapter": adapter,
        }

    @classmethod
    def _drop_loguru_default_handler(cls) -> None:
        if cls._dropped_default_handler:
            return
        cls._dropped_default_handler = True
        with contextlib.suppress(ValueError):
            logger.remove(0)

    @classmethod
    def _remove_handlers(cls, handler_ids: list[int]) -> None:
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
        config = dict(log_config or {})
        output = str(config.get("output", "stdout"))
        rotation_config = dict(config.get("rotation", {}))
        max_size = rotation_config.get("max_size", 100)

        cls.init(
            level="DEBUG" if debug else str(config.get("level", "INFO")),
            logger_name=logger_name,
            format=_validate_format(config.get("format")),
            log_file=None if output in ("stdout", "stderr", "") else output,
            rotation=f"{max_size} MB",
            retention=int(rotation_config.get("max_backups", 5)),
        )

    @classmethod
    def remove_logger(cls, logger_name: str) -> None:
        if logger_name in cls._configurations:
            cls._remove_handlers(cls._configurations[logger_name]["handler_ids"])
            del cls._configurations[logger_name]
            cls._emitter = None

    @classmethod
    def _emit(cls) -> Any:
        emitter = cls._emitter
        if emitter is None:
            cls._ensure_configured()
            emitter = cls._emitter = logger.opt(depth=cls._depth)
        return emitter

    @classmethod
    def _ensure_configured(cls, logger_name: str = "default"):
        if logger_name not in cls._configurations:
            cls.init(logger_name=logger_name)

    @classmethod
    def trace(cls, message: str, *args: Any, **kwargs: Any) -> None:
        cls._emit().trace(message, *args, **kwargs)

    @classmethod
    def debug(cls, message: str, *args: Any, **kwargs: Any) -> None:
        cls._emit().debug(message, *args, **kwargs)

    @classmethod
    def info(cls, message: str, *args: Any, **kwargs: Any) -> None:
        cls._emit().info(message, *args, **kwargs)

    @classmethod
    def success(cls, message: str, *args: Any, **kwargs: Any) -> None:
        cls._emit().success(message, *args, **kwargs)

    @classmethod
    def warning(cls, message: str, *args: Any, **kwargs: Any) -> None:
        cls._emit().warning(message, *args, **kwargs)

    @classmethod
    def error(cls, message: str, *args: Any, **kwargs: Any) -> None:
        cls._emit().error(message, *args, **kwargs)

    @classmethod
    def critical(cls, message: str, *args: Any, **kwargs: Any) -> None:
        cls._emit().critical(message, *args, **kwargs)

    @classmethod
    def exception(cls, message: str, *args: Any, **kwargs: Any) -> None:
        cls._emit().exception(message, *args, **kwargs)


log = LogUtil()
