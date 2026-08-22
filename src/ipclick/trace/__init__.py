"""请求链路记录：数据形状、进程计数器、SQLite 存储与对外门面。

原来是单个 1125 行的模块，里面装着五件互不相关的事。拆成四个子模块，
公开名字仍从 ``ipclick.trace`` 导出，调用方不需要改：

- :mod:`ipclick.trace.records`  —— 一条记录的形状、[TRACE] 配置、URL 脱敏
- :mod:`ipclick.trace.counters` —— 本进程的累计计数（不落盘，与链路记录无共同状态）
- :mod:`ipclick.trace.store`    —— SQLite 建表/迁移/异步写入/保留期/查询，含库文件认领协议
- :mod:`ipclick.trace.recorder` —— 门面与全局单例
"""

from ipclick.trace.counters import Counters
from ipclick.trace.recorder import (
    RequestTrace,
    TraceRecorder,
    get_recorder,
    init_recorder,
    reset_recorder,
)
from ipclick.trace.records import TraceRecord, TraceSettings, classify_status
from ipclick.trace.store import SQLiteSink, TraceReader


__all__ = [
    "Counters",
    "RequestTrace",
    "SQLiteSink",
    "TraceReader",
    "TraceRecord",
    "TraceRecorder",
    "TraceSettings",
    "classify_status",
    "get_recorder",
    "init_recorder",
    "reset_recorder",
]
