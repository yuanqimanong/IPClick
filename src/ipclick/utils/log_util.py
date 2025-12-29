import logging
import sys

try:
    # 尝试导入loguru
    from loguru import logger as loguru_logger

    has_loguru = True
except ImportError:
    loguru_logger = None
    has_loguru = False

_loguru_configured = False


def get_logger(name: str | None = None, level: str = "INFO"):
    """
    返回一个配置好的logger。
    如果loguru可用，使用loguru；否则使用标准logging。
    """
    if has_loguru and loguru_logger is not None:
        # 配置loguru（如果还没有配置过）
        _configure_loguru(level)
        if name:
            return loguru_logger.bind(name=name)
        return loguru_logger.bind(name="root")
    else:
        # 使用标准logging
        logger = logging.getLogger(name)
        # 避免重复添加handler
        if not logger.handlers:
            _configure_std_logging(logger, level)
        return logger


def _configure_loguru(level: str) -> None:
    """配置loguru日志器"""
    global _loguru_configured

    if loguru_logger is None or _loguru_configured:
        return

    loguru_logger.remove()
    _ = loguru_logger.add(
        sys.stderr,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{extra[name]}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "<level>{message}</level>"
        ),
        level=level,
        colorize=True,
        backtrace=True,  # 添加堆栈跟踪
        diagnose=True,  # 添加诊断信息
    )
    _loguru_configured = True


def _configure_std_logging(logger: logging.Logger, level: str) -> None:
    """配置标准logging"""
    logger.setLevel(level)
    # 创建控制台handler
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(level)
    # 设置格式
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    logger.propagate = False  # 避免重复输出


# 默认全局
default_logger = get_logger()


# 可以提供一个Mixin类，方便类继承
class LogMixin:
    """一个简单的Mixin，为类添加logger属性"""

    @property
    def logger(self):
        # 使用当前类的模块和类名作为logger的名称
        name = self.__class__.__module__ + "." + self.__class__.__name__
        return get_logger(name)
