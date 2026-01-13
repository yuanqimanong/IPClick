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
    """数据库适配器协议 - 允许用户自定义数据库输出"""

    def write(self, message: Any) -> None:
        """写入日志记录到数据库。

        Args:
            message: loguru 的 Message 对象，包含 .record (dict)
        """
        ...


class SQLiteAdapter:
    """SQLite 数据库适配器实现"""

    def __init__(self, db_path: str, table_name: str = "logs"):
        self.db_path: str = db_path
        self.table_name: str = table_name
        sql_path = PathUtil.resolve_path(db_path)
        PathUtil.ensure_parent_dir(sql_path)
        self.conn: Connection = sqlite3.connect(db_path, check_same_thread=False)
        self._create_table()

    def _create_table(self):
        """创建日志表，如果不存在"""
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

    def write(self, message: Any) -> None:
        """写入日志记录到 SQLite 数据库"""
        record = message.record

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
        """关闭数据库连接（可选，在程序结束时调用）"""
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


class LogUtil:
    """日志工具类"""

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
        if cls._configured:
            return
        logger.remove()

        console_format = (
            "[<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green>]"
            "<level>{level: <8}</level>"
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
            cls.info("日志文件配置", extra={"path": str(resolved_path)})

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
log = LogUtil
