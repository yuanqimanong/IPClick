import functools
import sqlite3
import sys
from pathlib import Path
from sqlite3 import Connection
from typing import Any, Callable, ClassVar, Protocol, TypeVar, cast

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
        self.db_path: str = db_path
        self.table_name: str = table_name
        sql_path = PathUtil.resolve_path(db_path)
        PathUtil.ensure_parent_dir(sql_path)
        self.conn: Connection = sqlite3.connect(
            db_path, check_same_thread=False, timeout=10.0
        )
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

    _configured: ClassVar[bool] = False

    @classmethod
    def init(
        cls,
        level: str = "INFO",
        *,
        format: str | None = None,
        log_file: str | Path | None = None,
        base_dir: Path | None = None,
        rotation: str = "10 MB",
        retention: str = "30 days",
        adapter: DatabaseAdapter | None = None,
        **kwargs: Any,
    ) -> None:
        """初始化日志工具配置

        配置日志记录的各种选项，包括日志级别、输出格式、文件输出和数据库输出等。

        Args:
            level: 日志级别，如 DEBUG, INFO, WARNING, ERROR 等
            format: 日志格式字符串，如果不指定则使用默认格式
            log_file: 日志文件路径，如果指定则同时输出到文件
            base_dir: 基础目录，用于构建日志文件路径
            rotation: 文件轮转大小，当日志文件达到此大小时创建新文件
            retention: 日志文件保留时间，超过此时间的旧文件会被删除
            adapter: 数据库适配器，如果指定则同时输出到数据库
            **kwargs: 传递给 loguru 的其他参数
        """
        if cls._configured:
            return
        logger.remove()

        console_format = (
            "[<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green>]"
            "<level> {level: <9}</level>"
            "[<cyan>{process.name}:{process}</cyan>]"
            "[<magenta>{thread.name}:{thread}</magenta>]"
            " <bold><yellow>{module}</yellow>:<yellow>{function}</yellow>:<underline>{line}</underline></bold> "
            "| <level>{message}</level>"
        )
        level = level.upper()
        console_handler = logger.add(
            sys.stderr,
            level=level,
            colorize=True,
            format=format or console_format,
            **kwargs,
        )

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

        if adapter:
            adapter_handler = logger.add(
                adapter.write,
                level=level,
                enqueue=True,
                **kwargs,
            )

        cls._configured = True

    @classmethod
    def _ensure_configured(cls):
        """确保日志工具已配置

        如果日志工具尚未配置，则使用默认设置进行初始化。
        """
        if not cls._configured:
            cls.init()

    # ==================== 核心日志方法 ====================

    @classmethod
    @ensure_configured
    def trace(cls, message: str, *args: Any, **kwargs: Any) -> None:
        logger.trace(message, *args, **kwargs)

    @classmethod
    @ensure_configured
    def debug(cls, message: str, *args: Any, **kwargs: Any) -> None:
        logger.debug(message, *args, **kwargs)

    @classmethod
    @ensure_configured
    def info(cls, message: str, *args: Any, **kwargs: Any) -> None:
        logger.info(message, *args, **kwargs)

    @classmethod
    @ensure_configured
    def success(cls, message: str, *args: Any, **kwargs: Any) -> None:
        logger.success(message, *args, **kwargs)

    @classmethod
    @ensure_configured
    def warning(cls, message: str, *args: Any, **kwargs: Any) -> None:
        logger.warning(message, *args, **kwargs)

    @classmethod
    @ensure_configured
    def error(cls, message: str, *args: Any, **kwargs: Any) -> None:
        logger.error(message, *args, **kwargs)

    @classmethod
    @ensure_configured
    def critical(cls, message: str, *args: Any, **kwargs: Any) -> None:
        logger.critical(message, *args, **kwargs)

    @classmethod
    @ensure_configured
    def exception(cls, message: str, *args: Any, **kwargs: Any) -> None:
        logger.exception(message, *args, **kwargs)


# 快捷方式
log = LogUtil()
