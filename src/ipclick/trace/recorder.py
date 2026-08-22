"""对外门面：跟踪一次请求的生命周期，并把结果分发给内存缓冲与 SQLite。"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
import sqlite3
import threading
import time
from typing import Any, final

from ipclick.trace.counters import Counters
from ipclick.trace.records import (
    TraceRecord,
    TraceSettings,
    default_node_id,
    host_only,
    host_only_in_text,
    matches,
)
from ipclick.trace.store import SQLiteSink
from ipclick.utils.log_util import log


WINDOW_CACHE_TTL = 10.0


@final
@dataclass
class RequestTrace:
    """请求执行期间由服务层逐步填充的可变链路上下文。"""

    adapter: str
    method: str
    uuid: str = ""
    url: str = ""
    stream: bool = False
    status_code: int = -1
    size: int = 0
    attempts: int = 1
    forwarded: bool = False
    queued_ms: int = 0
    error: str = ""
    node_id: str = ""


@final
class TraceRecorder:
    """协调进程计数、内存环形历史和可选 SQLite sink。"""

    def __init__(self, settings: TraceSettings | None = None) -> None:
        """按设置初始化内存与可选持久化记录器。"""
        self.settings: TraceSettings = settings or TraceSettings()
        self.node_id: str = self.settings.node_id or default_node_id()
        self.counters: Counters = Counters()
        self._recent: list[TraceRecord] = []
        self._recent_lock: threading.Lock = threading.Lock()
        self._window_cache: dict[int, tuple[float, dict[str, Any]]] = {}
        self._window_lock: threading.Lock = threading.Lock()
        self.sink: SQLiteSink | None = None

        if self.settings.sqlite_enabled:
            try:
                self.sink = SQLiteSink(
                    self.settings.sqlite_path,
                    retention_days=self.settings.retention_days,
                    queue_size=self.settings.queue_size,
                )
                retention = f"{self.settings.retention_days} 天" if self.settings.retention_days else "永久"
                log.info(f"链路记录落盘已启用: {self.sink.path}（保留 {retention}）")
            except (sqlite3.Error, OSError) as e:
                log.error(f"链路记录落盘启用失败，退回内存模式: {e}")
                self.sink = None

    @contextmanager
    def track_request(
        self, adapter: str, method: str, *, uuid: str = "", url: str = "", stream: bool = False
    ) -> Generator[RequestTrace]:
        """跟踪请求生命周期，并保证异常路径也产生完成记录。"""
        tr = RequestTrace(adapter=adapter, method=method, uuid=uuid, url=url, stream=stream, node_id=self.node_id)
        self.counters.enter()
        start = time.monotonic()
        try:
            yield tr
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            self.emit(tr, duration_ms)

    def emit(self, tr: RequestTrace, duration_ms: int) -> None:
        """冻结请求上下文，更新计数器并送入记录后端。"""
        # error 也要过一遍：适配器的错误信息里常嵌着完整 URL，只脱敏 url 字段等于
        # record_url = false 形同没设（查询串里的密钥照样落库、照样上页面）。
        full_url = self.settings.record_url
        url = tr.url if full_url else host_only(tr.url)
        error = tr.error if full_url else host_only_in_text(tr.error)
        record = TraceRecord(
            ts=time.time(),
            uuid=tr.uuid,
            node_id=tr.node_id or self.node_id,
            adapter=tr.adapter,
            method=tr.method,
            url=url,
            status_code=tr.status_code,
            duration_ms=duration_ms,
            size=tr.size,
            attempts=tr.attempts,
            forwarded=tr.forwarded,
            queued_ms=tr.queued_ms,
            error=error,
            stream=tr.stream,
        )
        self.counters.leave(record)
        self._push(record)

    def _push(self, record: TraceRecord) -> None:
        """按 only-errors 策略写入有界内存历史和异步 sink。"""
        if self.settings.only_errors and record.ok:
            return
        if self.settings.memory_size > 0:
            with self._recent_lock:
                self._recent.append(record)
                if len(self._recent) > self.settings.memory_size:
                    overflow = len(self._recent) - self.settings.memory_size
                    del self._recent[:overflow]
        if self.sink is not None:
            self.sink.submit(record)

    def record_retry(self, adapter: str, reason: str) -> None:
        """累计指定适配器的一次重试。"""
        self.counters.record_retry(f"{adapter}:{reason}" if adapter else reason)

    def record_rejected(self, reason: str) -> None:
        """累计一次服务端准入拒绝。"""
        self.counters.record_rejected(reason)

    def recent(
        self,
        limit: int = 100,
        *,
        status_class: str = "",
        adapter: str = "",
        keyword: str = "",
    ) -> list[TraceRecord]:
        """筛选并返回最新的内存记录副本。"""
        with self._recent_lock:
            records = list(reversed(self._recent))
        return [r for r in records if matches(r, status_class, adapter, keyword)][: max(1, limit)]

    def query(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        since: float | None = None,
        status_class: str = "",
        adapter: str = "",
        keyword: str = "",
    ) -> tuple[list[TraceRecord], str]:
        """优先查询健康的 SQLite sink，否则退回内存记录。"""
        if self.sink is not None and not self.sink.failed:
            return (
                self.sink.query(
                    limit=limit,
                    offset=offset,
                    since=since,
                    status_class=status_class,
                    adapter=adapter,
                    keyword=keyword,
                ),
                "sqlite",
            )
        if offset:
            records = self.recent(limit + offset, status_class=status_class, adapter=adapter, keyword=keyword)
            return records[offset:], "memory"
        return self.recent(limit, status_class=status_class, adapter=adapter, keyword=keyword), "memory"

    def status(self) -> dict[str, Any]:
        """返回记录后端、容量、丢弃量和数据库大小。"""
        with self._recent_lock:
            in_memory = len(self._recent)
        info: dict[str, Any] = {
            "node_id": self.node_id,
            "memory_size": self.settings.memory_size,
            "in_memory": in_memory,
            "only_errors": self.settings.only_errors,
            "sqlite_enabled": self.settings.sqlite_enabled,
            "source": "memory",
        }
        if self.sink is None:
            return info
        info.update(
            {
                "source": "memory" if self.sink.failed else "sqlite",
                "sqlite_path": self.sink.path,
                "sqlite_failed": self.sink.failed,
                "retention_days": self.sink.retention_days,
                "written": self.sink.written,
                "dropped": self.sink.dropped,
                "rows": self.sink.count(),
                "db_bytes": self.sink.db_size(),
            }
        )
        return info

    def stats(self, days: int = 30) -> dict[str, Any]:
        """组合实时进程计数与指定时间窗的持久化统计。"""
        out: dict[str, Any] = {"process": self.counters.snapshot(), "recorder": self.status()}
        if self.sink is not None and not self.sink.failed:
            out.update(self._window_stats(days))
        return out

    def _window_stats(self, days: int) -> dict[str, Any]:
        """短时缓存开销较高的 SQLite 窗口统计。"""
        now = time.monotonic()
        with self._window_lock:
            cached = self._window_cache.get(days)
            if cached is not None and now - cached[0] < WINDOW_CACHE_TTL:
                return cached[1]

        sink = self.sink
        if sink is None:
            return {}
        since = time.time() - days * 86400 if days > 0 else None
        data: dict[str, Any] = {
            "window_days": days,
            "window": sink.summary(since),
            "daily": sink.daily(days),
            "top_hosts": sink.top_hosts(since),
        }
        with self._window_lock:
            self._window_cache[days] = (now, data)
        return data

    def close(self) -> None:
        """关闭持久化 sink；内存快照仍可读取。"""
        if self.sink is not None:
            self.sink.close()


_recorder: TraceRecorder | None = None
_recorder_lock = threading.Lock()


def get_recorder() -> TraceRecorder:
    """线程安全地惰性创建全局记录器。"""
    global _recorder
    if _recorder is not None:
        return _recorder
    with _recorder_lock:
        if _recorder is None:
            _recorder = TraceRecorder()
    return _recorder


def init_recorder(settings: TraceSettings) -> TraceRecorder:
    """用新设置替换全局记录器并关闭旧 sink。"""
    global _recorder
    with _recorder_lock:
        old = _recorder
        _recorder = TraceRecorder(settings)
    if old is not None:
        old.close()
    return _recorder


def reset_recorder() -> None:
    """关闭并清空全局记录器，主要供进程退出和测试隔离使用。"""
    global _recorder
    with _recorder_lock:
        old = _recorder
        _recorder = None
    if old is not None:
        old.close()
