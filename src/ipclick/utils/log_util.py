from collections.abc import Callable
import contextlib
import functools
from pathlib import Path
import sqlite3
from sqlite3 import Connection
import sys
from typing import Any, ClassVar, Protocol, TypeVar, cast

from loguru import logger
from typing_extensions import runtime_checkable

from ipclick.utils.path_util import PathUtil


F = TypeVar("F", bound=Callable[..., Any])


@runtime_checkable
class DatabaseAdapter(Protocol):
    """数据库适配器协议 - 允许用户自定义数据库输出

    这个协议定义了一个标准接口，允许用户实现自定义的日志数据库适配器，
    以便将日志消息存储到不同的数据库系统中。
    """

    def write(self, log_message: Any) -> None:
        """写入日志记录到数据库。

        Args:
            log_message: loguru 的 Message 对象，包含 .record (dict)
        """
        ...


class SQLiteAdapter(DatabaseAdapter):
    """SQLite 数据库适配器实现

    这个类实现了将日志消息存储到 SQLite 数据库的功能。
    它遵循 DatabaseAdapter 协议，并提供了具体的 SQLite 实现。

    Attributes:
        db_path: SQLite 数据库文件路径
        table_name: 存储日志的表名
        conn: SQLite 数据库连接对象
    """

    def __init__(self, db_path: str, table_name: str = "logs"):
        """初始化 SQLite 适配器

        Args:
            db_path: SQLite 数据库文件路径
            table_name: 存储日志的表名，默认为 'logs'
        """
        # 表名会被拼进 SQL，不能来自不受信任的输入
        if not table_name.isidentifier():
            raise ValueError(f"非法的表名: {table_name!r}（只允许标识符字符）")

        self.db_path: str = db_path
        self.table_name: str = table_name
        # 之前算出了 sql_path 却仍用原始 db_path 建连接，相对路径的解析等于白做
        sql_path = PathUtil.resolve_path(db_path)
        PathUtil.ensure_parent_dir(sql_path)
        self.conn: Connection = sqlite3.connect(str(sql_path), check_same_thread=False, timeout=10.0)
        self._create_table()

    def _create_table(self):
        """创建日志表，如果不存在

        此方法会在数据库中创建一个日志表，如果该表尚不存在。
        表包含时间戳、日志级别、消息、文件位置、线程/进程ID等信息。
        """
        cursor = self.conn.cursor()
        cursor.execute(f"""
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

    def write(self, log_message: Any) -> None:
        """写入日志记录到 SQLite 数据库

        从 loguru 消息对象中提取日志记录信息并将其插入到 SQLite 数据库中。

        Args:
            log_message: loguru 的 Message 对象，包含 .record (dict)
        """
        record = log_message.record

        timestamp = record["time"].isoformat()  # 时间转换为 ISO 字符串
        level = record["level"].name
        message = record["message"]
        file = record["file"].path
        line = record["line"]
        function = record["function"]
        process_id = record["process"].id
        thread_id = record["thread"].id
        exception = str(record["exception"]) if record["exception"] else None

        with self.conn:
            self.conn.execute(
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
        """关闭数据库连接（可选，在程序结束时调用）

        关闭与 SQLite 数据库的连接。此方法应在应用程序结束时调用，
        以确保正确释放数据库资源。
        """
        self.conn.close()


def ensure_configured(func: F) -> F:
    """装饰器：确保日志工具已配置后再执行被装饰的方法

    此装饰器检查日志工具是否已配置，如果没有，则先进行配置。

    Args:
        func: 被装饰的函数

    Returns:
        装饰后的函数
    """

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


class LogUtil:
    """日志工具类

    提供统一的日志记录功能，封装了 loguru 库的功能。
    支持控制台输出、文件输出和数据库输出等多种日志记录方式。

    使用示例:
        1. 使用默认 log
            from ipclick.utils.log_util import log

        2. 初始化 log配置
            LogUtil.init(level="INFO", log_file="app.log")

            sqlite_adapter = SQLiteAdapter("logs/app.db")
            LogUtil.init(level="DEBUG", adapter=sqlite_adapter)

        3. 记录日志
            LogUtil.info("这是一条信息日志")
    """

    _configurations: ClassVar[dict[str, dict[str, Any]]] = {}
    _default_logger_name: ClassVar[str] = "default"
    _depth: ClassVar[int] = 2
    # 只移除本类自己注册的 handler。作为库，不能在 import 时 logger.remove()
    # 把宿主应用配置好的 loguru handler 一并清掉。
    _own_handler_ids: ClassVar[set[int]] = set()
    # loguru 自带的默认 handler 是否已摘除（见 _drop_loguru_default_handler）
    _dropped_default_handler: ClassVar[bool] = False

    def __init__(self, logger_name: str | None = None):
        """创建日志器实例

        Args:
            logger_name: 日志器名称，如果不指定则使用默认日志器
        """
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
        retention: str | int = "30 days",  # str 表示时间跨度，int 表示保留的文件个数
        adapter: DatabaseAdapter | None = None,
        **kwargs: Any,
    ) -> None:
        """初始化日志工具配置

        配置日志记录的各种选项，包括日志级别、输出格式、文件输出和数据库输出等。

        Args:
            level: 日志级别，如 DEBUG, INFO, WARNING, ERROR 等
            logger_name: 日志器名称，默认为 "default"
            format: 日志格式字符串，如果不指定则使用默认格式
            log_file: 日志文件路径，如果指定则同时输出到文件
            base_dir: 基础目录，用于构建日志文件路径
            rotation: 文件轮转大小，当日志文件达到此大小时创建新文件
            retention: 日志文件保留时间，超过此时间的旧文件会被删除
            adapter: 数据库适配器，如果指定则同时输出到数据库
            **kwargs: 传递给 loguru 的其他参数
        """
        console_format = (
            "[<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green>]"
            "<level> {level: <9}</level>"
            "[<cyan>{process.name}:{process}</cyan>]"
            "[<magenta>{thread.name}:{thread}</magenta>]"
            " <bold>[<yellow>{file}</yellow>]<yellow>{name}</yellow>:<yellow>{function}</yellow>:<underline>{line}</underline></bold> "
            "| <level>{message}</level>"
        )
        level = level.upper()

        cls._drop_loguru_default_handler()

        # 如果已经存在这个日志器的配置，先移除旧的handler
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
            resolved_path = PathUtil.resolve_path(log_file, base_dir)

            if not resolved_path.suffix:
                resolved_path = resolved_path.with_suffix(".log")

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

        # 保存配置
        cls._own_handler_ids.update(handler_ids)
        cls._configurations[logger_name] = {
            "handler_ids": handler_ids,
            "level": level,
            "adapter": adapter,
        }

    @classmethod
    def _drop_loguru_default_handler(cls) -> None:
        """移除 loguru 自带的默认 stderr handler（id 0），只做一次。

        loguru 在 import 时会自动装一个 DEBUG 级别的 stderr handler。如果不摘掉，
        我们再 add 一个就会每条日志打印两遍，而且 DEBUG 会绕过这里设置的级别。

        注意这跟"库在 import 时 logger.remove() 清空所有 handler"是两回事：
        那样会连宿主应用自己配的 handler 一起干掉；这里只在调用方主动初始化
        IPClick 日志时，摘掉 loguru 那个自动添加的默认项。约定俗成地，
        自行配置过 loguru 的应用都已经先 remove() 过了，此时 id 0 并不存在。
        """
        if cls._dropped_default_handler:
            return
        cls._dropped_default_handler = True
        with contextlib.suppress(ValueError):
            logger.remove(0)

    @classmethod
    def _remove_handlers(cls, handler_ids: list[int]) -> None:
        """移除本类注册过的 handler，忽略已被外部移除的。"""
        for handler_id in handler_ids:
            with contextlib.suppress(ValueError):  # 可能已被外部移除
                logger.remove(handler_id)
            cls._own_handler_ids.discard(handler_id)

    @classmethod
    def init_from_config(cls, log_config: dict[str, Any] | None, *, logger_name: str = "default") -> None:
        """按配置文件的 ``[LOG]`` 节初始化日志。

        原先 ``[LOG]`` 里的 level / output / rotation 从来没被读取过，改配置不生效。
        """
        config = dict(log_config or {})
        output = str(config.get("output", "stdout"))
        rotation_config = dict(config.get("rotation", {}))
        max_size = rotation_config.get("max_size", 100)

        cls.init(
            level=str(config.get("level", "INFO")),
            logger_name=logger_name,
            log_file=None if output in ("stdout", "stderr", "") else output,
            rotation=f"{max_size} MB",
            # max_backups 是"保留几个历史文件"，loguru 的 retention 传 int 正是此意
            retention=int(rotation_config.get("max_backups", 5)),
        )

    @classmethod
    def remove_logger(cls, logger_name: str) -> None:
        """移除指定名称的日志器配置

        Args:
            logger_name: 要移除的日志器名称
        """
        if logger_name in cls._configurations:
            cls._remove_handlers(cls._configurations[logger_name]["handler_ids"])
            del cls._configurations[logger_name]

    @classmethod
    def _ensure_configured(cls, logger_name: str = "default"):
        """确保日志器已配置

        Args:
            logger_name: 日志器名称
        """
        if logger_name not in cls._configurations:
            cls.init(logger_name=logger_name)

    # ==================== 核心日志方法 ====================

    @classmethod
    @ensure_configured
    def trace(cls, message: str, *args: Any, **kwargs: Any) -> None:
        logger.opt(depth=cls._depth).trace(message, *args, **kwargs)

    @classmethod
    @ensure_configured
    def debug(cls, message: str, *args: Any, **kwargs: Any) -> None:
        logger.opt(depth=cls._depth).debug(message, *args, **kwargs)

    @classmethod
    @ensure_configured
    def info(cls, message: str, *args: Any, **kwargs: Any) -> None:
        logger.opt(depth=cls._depth).info(message, *args, **kwargs)

    @classmethod
    @ensure_configured
    def success(cls, message: str, *args: Any, **kwargs: Any) -> None:
        logger.opt(depth=cls._depth).success(message, *args, **kwargs)

    @classmethod
    @ensure_configured
    def warning(cls, message: str, *args: Any, **kwargs: Any) -> None:
        logger.opt(depth=cls._depth).warning(message, *args, **kwargs)

    @classmethod
    @ensure_configured
    def error(cls, message: str, *args: Any, **kwargs: Any) -> None:
        logger.opt(depth=cls._depth).error(message, *args, **kwargs)

    @classmethod
    @ensure_configured
    def critical(cls, message: str, *args: Any, **kwargs: Any) -> None:
        logger.opt(depth=cls._depth).critical(message, *args, **kwargs)

    @classmethod
    @ensure_configured
    def exception(cls, message: str, *args: Any, **kwargs: Any) -> None:
        logger.opt(depth=cls._depth).exception(message, *args, **kwargs)


# 快捷方式
log = LogUtil()
