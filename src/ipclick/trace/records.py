"""链路记录的数据形状：一条记录、它的配置、以及 URL 脱敏。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
import re
import sqlite3
from typing import Any, final

from ipclick.utils.coerce import as_bool, as_int, as_text
from ipclick.utils.log_util import log


DEFAULT_MEMORY_SIZE = 500

DEFAULT_QUEUE_SIZE = 5000

_URL_MAX_LEN = 512


def classify_status(status_code: int) -> str:
    """把最终状态码归入界面和查询共用的状态分类。"""
    if status_code < 200:
        return "failure"
    if status_code < 300:
        return "2xx"
    if status_code < 400:
        return "3xx"
    if status_code < 500:
        return "4xx"
    return "5xx"


@final
@dataclass(frozen=True, slots=True)
class TraceRecord:
    """一条不可变的已完成请求链路记录。"""

    ts: float
    uuid: str
    node_id: str
    adapter: str
    method: str
    url: str
    status_code: int
    duration_ms: int
    size: int
    attempts: int = 1
    forwarded: bool = False
    queued_ms: int = 0
    error: str = ""
    stream: bool = False

    @property
    def host(self) -> str:
        """返回用于限流和站点排行的目标主机。"""
        from ipclick.limiter import host_of

        return host_of(self.url) or self.url or "-"

    @property
    def ok(self) -> bool:
        """将 2xx 与 3xx 视为执行成功。"""
        return 200 <= self.status_code < 400

    @property
    def status_class(self) -> str:
        """返回可筛选的状态类别。"""
        return classify_status(self.status_code)

    @property
    def when(self) -> str:
        """按服务端本地时区格式化展示时间。"""
        return datetime.fromtimestamp(self.ts).strftime("%Y-%m-%d %H:%M:%S")

    @property
    def iso(self) -> str:
        """返回带本地时区偏移的 ISO 时间。"""
        return datetime.fromtimestamp(self.ts).astimezone().isoformat(timespec="seconds")

    def as_row(self) -> tuple[Any, ...]:
        """转换为与 SQLite 插入列顺序一致的元组，并限制敏感长文本。"""
        return (
            self.ts,
            self.uuid,
            self.node_id,
            self.adapter,
            self.method,
            self.url[:_URL_MAX_LEN],
            self.status_code,
            self.duration_ms,
            self.size,
            self.attempts,
            1 if self.forwarded else 0,
            self.queued_ms,
            self.error[:500],
            1 if self.stream else 0,
            self.host,
        )

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> TraceRecord:
        """从 SQLite 行恢复链路记录。"""
        return cls(
            ts=float(row["ts"]),
            uuid=str(row["uuid"] or ""),
            node_id=str(row["node_id"] or ""),
            adapter=str(row["adapter"] or ""),
            method=str(row["method"] or ""),
            url=str(row["url"] or ""),
            status_code=int(row["status_code"]),
            duration_ms=int(row["duration_ms"] or 0),
            size=int(row["size"] or 0),
            attempts=int(row["attempts"] or 1),
            forwarded=bool(row["forwarded"]),
            queued_ms=int(row["queued_ms"] or 0),
            error=str(row["error"] or ""),
            stream=bool(row["stream"]),
        )


@final
@dataclass(frozen=True, slots=True)
class TraceSettings:
    """链路内存缓冲、落盘队列和保留策略。"""

    memory_size: int = DEFAULT_MEMORY_SIZE
    sqlite_enabled: bool = False
    sqlite_path: str = "ipclick-trace.db"
    retention_days: int = 30
    only_errors: bool = False
    queue_size: int = DEFAULT_QUEUE_SIZE
    record_url: bool = True
    node_id: str = ""

    @classmethod
    def from_config(cls, section: dict[str, Any], node_id: str = "") -> TraceSettings:
        """容错解析 ``[TRACE]`` 配置，并对非法整数给出警告。"""
        defaults = cls()

        def _int(key: str, default: int, minimum: int) -> int:
            """读取单个非负整数并在回退默认值时解释原因。"""
            if key not in section:
                return default
            raw = section[key]
            value = as_int(raw, default, minimum=minimum)
            if value == default and raw != default:
                log.warning(f"[TRACE].{key} 不是 >= {minimum} 的整数，改用默认值 {default}")
            return value

        return cls(
            memory_size=_int("memory_size", defaults.memory_size, 0),
            sqlite_enabled=as_bool(section.get("sqlite_enabled"), defaults.sqlite_enabled),
            sqlite_path=as_text(section.get("sqlite_path"), defaults.sqlite_path),
            retention_days=_int("retention_days", defaults.retention_days, 0),
            only_errors=as_bool(section.get("only_errors"), defaults.only_errors),
            queue_size=_int("queue_size", defaults.queue_size, 100),
            record_url=as_bool(section.get("record_url"), defaults.record_url),
            node_id=node_id or as_text(section.get("node_id")),
        )


def matches(record: TraceRecord, status_class: str, adapter: str, keyword: str) -> bool:
    """判断内存记录是否满足与 SQLite 查询一致的过滤条件。"""
    if adapter and record.adapter != adapter:
        return False
    if keyword and keyword.lower() not in record.url.lower():
        return False
    if not status_class:
        return True
    if status_class in ("failed", "error"):
        return not record.ok
    return record.status_class == status_class


def host_only(url: str) -> str:
    """在关闭完整 URL 记录时仅保留 hostname。"""
    from ipclick.limiter import host_of

    return host_of(url) or ""


# 自由文本里的 URL。右边界排掉常见的收尾标点与中文括号，免得把"）"之类也吃进来。
_URL_IN_TEXT = re.compile(r"https?://[^\s'\"<>）)\]]+", re.IGNORECASE)


def host_only_in_text(text: str) -> str:
    """把自由文本里出现的完整 URL 缩成 host。

    ``record_url = false`` 承诺"只记 host"，但原来只对 url 字段生效，error 字段是原样
    抄进去的——而适配器的错误信息里经常嵌着完整 URL（重定向超上限、浏览器导航失败都会
    带上）。于是 ``?api_key=…`` 照样落进 SQLite，并显示在请求流页面上。
    """
    if not text:
        return text
    return _URL_IN_TEXT.sub(lambda m: host_only(m.group(0)) or m.group(0), text)


def default_node_id() -> str:
    """以主机名和 PID 生成单进程可区分的默认节点标识。"""
    import socket

    try:
        host = socket.gethostname()
    except OSError:
        host = "unknown"
    return f"{host}:{os.getpid()}"
