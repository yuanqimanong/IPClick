"""
log_util.py - Pythonic日志工具封装

设计原则：
1. 明确优于隐晦 - 清晰的接口和类型提示
2. 简单优于复杂 - 简化的配置接口
3. 可读性至上 - 清晰的函数名和文档
4. 错误不应静默 - 合理的默认值和容错处理
5. 实用性高于纯粹性 - 提供灵活的可扩展接口

注意：使用Python 3.11+的类型提示语法
"""

import sys
import os
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, Callable, ClassVar, Protocol, runtime_checkable
from datetime import datetime
import inspect

# 延迟导入，避免强制依赖
try:
    from loguru import logger as _loguru_logger

    LOGURU_AVAILABLE = True
except ImportError:
    LOGURU_AVAILABLE = False
    _loguru_logger = None

try:
    from rich.console import Console
    from rich.traceback import Traceback
    from rich.text import Text
    from rich.table import Table

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False


@runtime_checkable
class DatabaseAdapter(Protocol):
    """数据库适配器协议 - 允许用户自定义数据库输出"""

    def write(self, record: dict[str, Any]) -> None:
        """写入日志记录到数据库。

        Args:
            record: 日志记录字典，包含时间、级别、消息等信息
        """
        ...


class LogUtil:
    """Pythonic日志工具类。

    封装loguru并提供简洁易用的接口，支持：
    - 控制台和文件日志输出
    - 可选的Rich美化输出
    - 数据库适配器接口
    - 上下文管理器和装饰器
    - 增强的错误信息记录

    Attributes:
        _configured: 是否已配置日志系统
        _rich_console: Rich控制台实例（如果可用）
        _database_adapters: 数据库适配器列表
    """

    _configured: ClassVar[bool] = False
    _rich_console: ClassVar[Console | None] = None
    _database_adapters: ClassVar[list[DatabaseAdapter]] = []

    @classmethod
    def init(
        cls,
        log_file: str | Path | None = None,
        base_dir: Path | None = None,
        rotation: str = "10 MB",
        retention: str = "30 days",
        level: str = "INFO",
        console_format: str | None = None,
        file_format: str | None = None,
        use_rich: bool | None = None,
        **kwargs,
    ) -> None:
        """初始化日志配置。

        如果未指定use_rich，将自动检测rich是否可用并启用。

        Args:
            log_file: 日志文件路径，None表示仅输出到控制台
            base_dir: 基础目录，用于解析相对路径的log_file
            rotation: 日志轮转规则，如"10 MB"或"1 day"
            retention: 日志保留时间，如"30 days"
            level: 日志级别，如"DEBUG", "INFO", "WARNING"
            console_format: 控制台输出格式字符串
            file_format: 文件输出格式字符串
            use_rich: 是否使用rich美化控制台输出，None为自动检测
            **kwargs: 传递给loguru的额外参数

        Raises:
            ValueError: 如果log_file是相对路径但未提供base_dir
        """
        if not LOGURU_AVAILABLE:
            cls._fallback_logging()
            return

        # 移除默认配置，避免重复
        _loguru_logger.remove()

        # 初始化rich控制台（如果可用）
        if use_rich is None:
            use_rich = RICH_AVAILABLE
        elif use_rich and not RICH_AVAILABLE:
            cls._warn_once("rich不可用，将使用普通控制台输出")
            use_rich = False

        if use_rich:
            cls._rich_console = Console()
            console_sink = cls._rich_handler
        else:
            cls._rich_console = None
            console_sink = sys.stderr

        # 配置控制台输出
        console_fmt = console_format or cls._get_format(
            include_pid=False,
            include_thread=False,
            colorful=True,
        )

        _loguru_logger.add(
            console_sink,
            format=console_fmt,
            level=level.upper(),
            colorize=not use_rich,  # rich自己处理颜色
            **kwargs,
        )

        # 配置文件输出（如果提供文件路径）
        if log_file:
            # 解析路径
            log_path = cls._resolve_path(log_file, base_dir)
            log_path.parent.mkdir(parents=True, exist_ok=True)

            # 文件格式包含更多调试信息
            file_fmt = file_format or cls._get_format(
                include_pid=True,
                include_thread=True,
                colorful=False,
            )

            _loguru_logger.add(
                str(log_path),
                format=file_fmt,
                level=level.upper(),
                rotation=rotation,
                retention=retention,
                compression="zip",
                **kwargs,
            )

        cls._configured = True
        cls.info("日志系统初始化完成", extra={"level": level})
        if log_file:
            cls.info("日志文件配置", extra={"path": str(log_path)})

    @staticmethod
    def _resolve_path(path: str | Path, base_dir: Path | None) -> Path:
        """解析文件路径，支持相对路径和绝对路径。

        Args:
            path: 文件路径，可以是相对或绝对
            base_dir: 基础目录，用于解析相对路径

        Returns:
            Path: 解析后的绝对路径

        Raises:
            ValueError: 如果是相对路径但未提供base_dir
        """
        path_obj = Path(path)

        if path_obj.is_absolute():
            return path_obj

        # 相对路径需要base_dir
        if base_dir is None:
            # 默认使用当前工作目录
            base_dir = Path.cwd()

        return base_dir / path_obj

    @staticmethod
    def _get_format(
        include_pid: bool = False,
        include_thread: bool = False,
        colorful: bool = True,
    ) -> str:
        """获取日志格式字符串。

        Args:
            include_pid: 是否包含进程ID
            include_thread: 是否包含线程信息
            colorful: 是否包含颜色标签

        Returns:
            格式字符串
        """
        # 时间部分
        time_tag = "green" if colorful else ""
        time_part = f"<{time_tag}>{{time:YYYY-MM-DD HH:mm:ss.SSS}}</{time_tag}>"

        # 级别部分
        level_part = "<level>{level: <8}</level>"

        # 代码位置部分
        location_tag = "cyan" if colorful else ""
        location_part = (
            f"<{location_tag}>{{name}}</{location_tag}>:"
            f"<{location_tag}>{{function}}</{location_tag}>:"
            f"<{location_tag}>{{line}}</{location_tag}>"
        )

        # 进程/线程信息
        extra_parts = []
        if include_pid:
            pid_tag = "magenta" if colorful else ""
            extra_parts.append(f"<{pid_tag}>PID:{{process}}</{pid_tag}>")
        if include_thread:
            thread_tag = "yellow" if colorful else ""
            extra_parts.append(f"<{thread_tag}>TID:{{thread}}</{thread_tag}>")

        # 消息部分
        message_part = "<level>{message}</level>"

        # 组合所有部分
        parts = [time_part, level_part, location_part] + extra_parts + [message_part]
        return " | ".join(parts)

    @classmethod
    def _rich_handler(cls, message: Any) -> None:
        """Rich处理函数，用于美化控制台输出。"""
        if cls._rich_console:
            record = message.record

            # 创建带样式的文本
            time_str = record["time"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            time_text = Text(time_str, style="green")

            level_style = {
                "DEBUG": "dim blue",
                "INFO": "blue",
                "WARNING": "yellow",
                "ERROR": "red",
                "CRITICAL": "bold red",
            }.get(record["level"].name, "white")

            level_text = Text(f"{record['level'].name: <8}", style=level_style)

            location = f"{record['name']}:{record['function']}:{record['line']}"
            location_text = Text(location, style="cyan")

            msg_text = Text(str(record["message"]), style="white")

            # 组合输出
            cls._rich_console.print(
                time_text,
                level_text,
                location_text,
                msg_text,
                sep=" | ",
            )

    @classmethod
    def _fallback_logging(cls) -> None:
        """loguru不可用时的备用方案。"""
        import logging

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )
        cls._configured = True
        cls.warning("loguru未安装，使用标准logging模块")

    @classmethod
    def _warn_once(cls, message: str) -> None:
        """只警告一次，避免重复输出。"""
        if not hasattr(cls, "_warnings_shown"):
            cls._warnings_shown = set()

        if message not in cls._warnings_shown:
            cls.warning(message)
            cls._warnings_shown.add(message)

    @classmethod
    def _ensure_configured(cls) -> None:
        """确保日志已配置（惰性初始化）。"""
        if not cls._configured:
            cls.init()

    # ==================== 核心日志方法 ====================

    @classmethod
    def debug(cls, message: str, **kwargs: Any) -> None:
        """记录调试信息。

        Args:
            message: 日志消息
            **kwargs: 额外参数，会添加到日志记录中
        """
        cls._ensure_configured()
        if LOGURU_AVAILABLE:
            _loguru_logger.debug(message, **kwargs)
            cls._write_to_databases("DEBUG", message, kwargs)
        else:
            import logging

            logging.debug(message, **kwargs)

    @classmethod
    def info(cls, message: str, **kwargs: Any) -> None:
        """记录一般信息。

        Args:
            message: 日志消息
            **kwargs: 额外参数，会添加到日志记录中
        """
        cls._ensure_configured()
        if LOGURU_AVAILABLE:
            _loguru_logger.info(message, **kwargs)
            cls._write_to_databases("INFO", message, kwargs)
        else:
            import logging

            logging.info(message, **kwargs)

    @classmethod
    def warning(cls, message: str, **kwargs: Any) -> None:
        """记录警告信息。

        Args:
            message: 日志消息
            **kwargs: 额外参数，会添加到日志记录中
        """
        cls._ensure_configured()
        if LOGURU_AVAILABLE:
            _loguru_logger.warning(message, **kwargs)
            cls._write_to_databases("WARNING", message, kwargs)
        else:
            import logging

            logging.warning(message, **kwargs)

    @classmethod
    def error(
        cls,
        message: str,
        exception: Exception | None = None,
        context_vars: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """记录错误信息，可附加异常和上下文变量。

        Args:
            message: 日志消息
            exception: 关联的异常对象
            context_vars: 上下文变量字典，用于调试
            **kwargs: 额外参数
        """
        cls._ensure_configured()

        # 增强错误消息
        full_message = message
        if exception:
            full_message = f"{message} - {type(exception).__name__}: {str(exception)}"

        if context_vars:
            vars_str = ", ".join(f"{k}={v!r}" for k, v in context_vars.items())
            full_message = f"{full_message} [上下文: {vars_str}]"

        if LOGURU_AVAILABLE:
            if exception and cls._rich_console:
                # 使用rich输出详细的异常信息
                cls._log_exception_with_context(full_message, exception, context_vars)
            else:
                _loguru_logger.error(full_message, **kwargs)

            cls._write_to_databases("ERROR", full_message, kwargs)
        else:
            import logging

            logging.error(full_message, **kwargs)

    @classmethod
    def critical(cls, message: str, **kwargs: Any) -> None:
        """记录严重错误信息。

        Args:
            message: 日志消息
            **kwargs: 额外参数
        """
        cls._ensure_configured()
        if LOGURU_AVAILABLE:
            _loguru_logger.critical(message, **kwargs)
            cls._write_to_databases("CRITICAL", message, kwargs)
        else:
            import logging

            logging.critical(message, **kwargs)

    @classmethod
    def exception(
        cls,
        message: str,
        exc: Exception,
        capture_locals: bool = True,
        **kwargs: Any,
    ) -> None:
        """记录异常信息（包含堆栈跟踪）。

        Args:
            message: 日志消息
            exc: 异常对象
            capture_locals: 是否捕获局部变量
            **kwargs: 额外参数
        """
        cls._ensure_configured()

        if LOGURU_AVAILABLE:
            if cls._rich_console and capture_locals:
                # 使用rich输出带局部变量的异常跟踪
                cls._log_exception_with_locals(message, exc)
            else:
                _loguru_logger.exception(
                    f"{message} - {type(exc).__name__}: {str(exc)}"
                )

            cls._write_to_databases(
                "ERROR", f"{message} - {type(exc).__name__}", kwargs
            )
        else:
            import logging

            logging.exception(f"{message} - {type(exc).__name__}: {str(exc)}")

    @classmethod
    def _log_exception_with_context(
        cls,
        message: str,
        exc: Exception,
        context_vars: dict[str, Any] | None,
    ) -> None:
        """使用rich记录带有上下文变量的异常。"""
        if not cls._rich_console:
            return

        # 创建表格显示上下文变量
        if context_vars:
            table = Table(
                title="上下文变量", show_header=True, header_style="bold magenta"
            )
            table.add_column("变量名", style="cyan")
            table.add_column("值", style="white")

            for key, value in context_vars.items():
                # 截断过长的值
                str_value = str(value)
                if len(str_value) > 100:
                    str_value = str_value[:97] + "..."
                table.add_row(key, str_value)

            cls._rich_console.print(table)

        # 显示异常跟踪
        tb = Traceback.from_exception(type(exc), exc, exc.__traceback__)
        cls._rich_console.print(tb)
        cls._rich_console.print(Text(message, style="bold red"))

    @classmethod
    def _log_exception_with_locals(cls, message: str, exc: Exception) -> None:
        """使用rich记录带局部变量的异常。"""
        if not cls._rich_console:
            return

        # 获取异常发生时的帧信息
        tb = exc.__traceback__
        while tb is not None:
            frame = tb.tb_frame
            locals_dict = frame.f_locals.copy()

            # 创建局部变量表格
            if locals_dict:
                table = Table(
                    title=f"局部变量 @ {frame.f_code.co_name}",
                    show_header=True,
                    header_style="bold cyan",
                )
                table.add_column("变量名", style="green")
                table.add_column("类型", style="yellow")
                table.add_column("值", style="white")

                for key, value in locals_dict.items():
                    # 避免敏感信息泄露
                    if key.startswith("_"):
                        continue

                    type_name = type(value).__name__
                    str_value = str(value)
                    if len(str_value) > 50:
                        str_value = str_value[:47] + "..."

                    table.add_row(key, type_name, str_value)

                cls._rich_console.print(table)

            tb = tb.tb_next

        # 显示异常跟踪
        tb_display = Traceback.from_exception(type(exc), exc, exc.__traceback__)
        cls._rich_console.print(tb_display)
        cls._rich_console.print(Text(message, style="bold red"))

    @classmethod
    def _write_to_databases(
        cls, level: str, message: str, extra: dict[str, Any]
    ) -> None:
        """将日志写入所有数据库适配器。"""
        if not cls._database_adapters:
            return

        record = {
            "timestamp": datetime.now(),
            "level": level,
            "message": message,
            "extra": extra,
        }

        for adapter in cls._database_adapters:
            try:
                adapter.write(record)
            except Exception as e:
                # 避免循环记录
                if LOGURU_AVAILABLE:
                    _loguru_logger.error(f"数据库适配器写入失败: {e}")

    # ==================== 数据库适配器接口 ====================

    @classmethod
    def add_database_adapter(cls, adapter: DatabaseAdapter) -> None:
        """添加数据库适配器。

        Args:
            adapter: 实现了DatabaseAdapter协议的对象
        """
        if not isinstance(adapter, DatabaseAdapter):
            raise TypeError("适配器必须实现DatabaseAdapter协议")

        cls._database_adapters.append(adapter)
        cls.info("数据库适配器已添加", extra={"adapter_type": type(adapter).__name__})

    @classmethod
    def remove_database_adapter(cls, adapter: DatabaseAdapter) -> bool:
        """移除数据库适配器。

        Args:
            adapter: 要移除的适配器

        Returns:
            bool: 是否成功移除
        """
        try:
            cls._database_adapters.remove(adapter)
            cls.info(
                "数据库适配器已移除", extra={"adapter_type": type(adapter).__name__}
            )
            return True
        except ValueError:
            return False

    # ==================== 上下文管理器和装饰器 ====================

    @classmethod
    @contextmanager
    def catch(
        cls,
        message: str = "操作失败",
        reraise: bool = True,
        capture_locals: bool = False,
    ):
        """上下文管理器：捕获并记录异常。

        Args:
            message: 异常时的日志消息
            reraise: 是否重新抛出异常
            capture_locals: 是否捕获局部变量

        Yields:
            None
        """
        try:
            yield
        except Exception as e:
            # 获取调用者的局部变量
            context_vars = None
            if capture_locals:
                try:
                    # 获取上一帧的局部变量
                    frame = inspect.currentframe()
                    if frame and frame.f_back:
                        context_vars = {
                            k: v
                            for k, v in frame.f_back.f_locals.items()
                            if not k.startswith("_")
                        }
                except Exception:
                    context_vars = None

            cls.exception(message, e, capture_locals=capture_locals)

            if reraise:
                raise

    @classmethod
    def logged(cls, level: str = "info", capture_locals: bool = False):
        """装饰器：自动记录函数执行。

        Args:
            level: 日志级别
            capture_locals: 发生异常时是否捕获局部变量

        Returns:
            装饰器函数
        """

        def decorator(func: Callable):
            @wraps(func)
            def wrapper(*args, **kwargs):
                log_method = getattr(cls, level.lower(), cls.info)

                # 记录函数开始
                func_name = func.__name__
                log_method(f"开始执行: {func_name}")

                try:
                    result = func(*args, **kwargs)
                    # 记录函数成功完成
                    log_method(f"完成执行: {func_name}")
                    return result
                except Exception as e:
                    # 记录函数失败
                    context_vars = None
                    if capture_locals:
                        # 捕获函数参数作为上下文
                        context_vars = {
                            "args": args,
                            "kwargs": kwargs,
                        }

                    cls.error(
                        f"执行失败: {func_name}",
                        exception=e,
                        context_vars=context_vars,
                    )
                    raise

            return wrapper

        return decorator

    @classmethod
    def catch_errors(cls, *methods: str, capture_locals: bool = False):
        """类装饰器：为指定方法添加异常捕获。

        Args:
            *methods: 要装饰的方法名，如果不指定则装饰所有公共方法
            capture_locals: 是否捕获局部变量

        Returns:
            类装饰器
        """

        def class_decorator(cls_obj):
            # 确定要装饰的方法
            methods_to_decorate = methods
            if not methods_to_decorate:
                # 如果没有指定方法，装饰所有公共方法
                methods_to_decorate = [
                    name
                    for name, value in cls_obj.__dict__.items()
                    if callable(value) and not name.startswith("_")
                ]

            # 为每个方法添加装饰器
            for method_name in methods_to_decorate:
                if hasattr(cls_obj, method_name):
                    method = getattr(cls_obj, method_name)
                    decorated = cls.logged(capture_locals=capture_locals)(method)
                    setattr(cls_obj, method_name, decorated)

            return cls_obj

        return class_decorator

    @classmethod
    def performance(cls, threshold: float = 1.0):
        """装饰器：记录函数执行时间。

        Args:
            threshold: 超过此秒数才记录警告

        Returns:
            装饰器函数
        """

        def decorator(func: Callable):
            @wraps(func)
            def wrapper(*args, **kwargs):
                import time

                start = time.perf_counter()

                try:
                    result = func(*args, **kwargs)
                    return result
                finally:
                    elapsed = time.perf_counter() - start
                    if elapsed > threshold:
                        cls.warning(
                            f"函数 {func.__name__} 执行较慢",
                            extra={
                                "elapsed": f"{elapsed:.2f}s",
                                "threshold": f"{threshold}s",
                            },
                        )
                    else:
                        cls.debug(
                            f"函数 {func.__name__} 执行时间",
                            extra={"elapsed": f"{elapsed:.2f}s"},
                        )

            return wrapper

        return decorator


# 创建全局别名，便于导入
log = LogUtil

"""
database_adapters.py - 数据库适配器示例
"""

import sqlite3
from typing import Any
from datetime import datetime


class SQLiteAdapter(DatabaseAdapter):
    """SQLite数据库适配器示例。"""

    def __init__(self, db_path: str = "logs.db"):
        """初始化SQLite适配器。

        Args:
            db_path: SQLite数据库文件路径
        """
        self.db_path = db_path
        self._init_database()

    def _init_database(self) -> None:
        """初始化数据库表。"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                extra TEXT
            )
        """)

        # 创建索引
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_level ON logs(level)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON logs(timestamp)")

        conn.commit()
        conn.close()

    def write(self, record: dict[str, Any]) -> None:
        """写入日志记录到数据库。

        Args:
            record: 日志记录字典
        """
        import json

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
                       INSERT INTO logs (timestamp, level, message, extra)
                       VALUES (?, ?, ?, ?)
                       """, (
                           record["timestamp"],
                           record["level"],
                           record["message"],
                           json.dumps(record.get("extra", {}), default=str),
                       ))

        conn.commit()
        conn.close()


class ConsoleDatabaseAdapter(DatabaseAdapter):
    """控制台输出适配器（用于演示）。"""

    def write(self, record: dict[str, Any]) -> None:
        """将日志记录输出到控制台。

        Args:
            record: 日志记录字典
        """
        print(f"[数据库适配器] {record['timestamp']} - {record['level']}: {record['message']}")