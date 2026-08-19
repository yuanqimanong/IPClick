from __future__ import annotations

from collections.abc import Generator, Sequence
import contextlib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import queue
import sqlite3
import sys
import threading
import time
from typing import Any, final

from ipclick.utils.coerce import as_bool, as_int, as_text
from ipclick.utils.log_util import log


DEFAULT_MEMORY_SIZE = 500

DEFAULT_QUEUE_SIZE = 5000

_BATCH_SIZE = 200

_FLUSH_INTERVAL = 0.5

_RETENTION_INTERVAL = 3600.0

WINDOW_CACHE_TTL = 10.0

_URL_MAX_LEN = 512


def classify_status(status_code: int) -> str:
    if status_code < 0:
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
        from ipclick.limiter import host_of

        return host_of(self.url) or self.url or "-"

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    @property
    def status_class(self) -> str:
        return classify_status(self.status_code)

    @property
    def when(self) -> str:
        return datetime.fromtimestamp(self.ts).strftime("%Y-%m-%d %H:%M:%S")

    @property
    def iso(self) -> str:
        return datetime.fromtimestamp(self.ts).astimezone().isoformat(timespec="seconds")

    def as_row(self) -> tuple[Any, ...]:
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
        defaults = cls()

        def _int(key: str, default: int, minimum: int) -> int:
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


@final
@dataclass
class _AdapterStat:
    total: int = 0
    ok: int = 0
    duration_ms: int = 0
    bytes: int = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "ok": self.ok,
            "failed": self.total - self.ok,
            "avg_ms": round(self.duration_ms / self.total, 1) if self.total else 0.0,
            "bytes": self.bytes,
        }


@final
class Counters:
    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        self.started_at: float = time.time()
        self.total: int = 0
        self.ok: int = 0
        self.in_flight: int = 0
        self.peak_in_flight: int = 0
        self.duration_ms: int = 0
        self.bytes: int = 0
        self.by_status: dict[str, int] = {}
        self.by_adapter: dict[str, _AdapterStat] = {}
        self.retries: dict[str, int] = {}
        self.rejected: dict[str, int] = {}

    def enter(self) -> None:
        with self._lock:
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)

    def leave(self, record: TraceRecord) -> None:
        with self._lock:
            self.in_flight = max(0, self.in_flight - 1)
            self.total += 1
            self.duration_ms += record.duration_ms
            self.bytes += record.size
            cls_ = record.status_class
            self.by_status[cls_] = self.by_status.get(cls_, 0) + 1
            stat = self.by_adapter.get(record.adapter)
            if stat is None:
                stat = _AdapterStat()
                self.by_adapter[record.adapter] = stat
            stat.total += 1
            stat.duration_ms += record.duration_ms
            stat.bytes += record.size
            if record.ok:
                self.ok += 1
                stat.ok += 1

    def record_retry(self, reason: str) -> None:
        with self._lock:
            self.retries[reason] = self.retries.get(reason, 0) + 1

    def record_rejected(self, reason: str) -> None:
        with self._lock:
            self.rejected[reason] = self.rejected.get(reason, 0) + 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            total = self.total
            return {
                "started_at": self.started_at,
                "uptime_seconds": int(time.time() - self.started_at),
                "total": total,
                "ok": self.ok,
                "failed": total - self.ok,
                "success_rate": round(self.ok / total * 100, 1) if total else 0.0,
                "avg_ms": round(self.duration_ms / total, 1) if total else 0.0,
                "bytes": self.bytes,
                "in_flight": self.in_flight,
                "peak_in_flight": self.peak_in_flight,
                "by_status": dict(sorted(self.by_status.items())),
                "by_adapter": {k: v.snapshot() for k, v in sorted(self.by_adapter.items())},
                "retries": dict(sorted(self.retries.items())),
                "rejected": dict(sorted(self.rejected.items())),
            }


_COLUMNS = (
    "ts",
    "uuid",
    "node_id",
    "adapter",
    "method",
    "url",
    "status_code",
    "duration_ms",
    "size",
    "attempts",
    "forwarded",
    "queued_ms",
    "error",
    "stream",
    "host",
)

_INSERT_SQL = f"INSERT INTO traces ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' * len(_COLUMNS))})"

_SCHEMA_TABLE = """
CREATE TABLE IF NOT EXISTS traces (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    uuid        TEXT,
    node_id     TEXT,
    adapter     TEXT,
    method      TEXT,
    url         TEXT,
    status_code INTEGER NOT NULL,
    duration_ms INTEGER,
    size        INTEGER,
    attempts    INTEGER,
    forwarded   INTEGER,
    queued_ms   INTEGER,
    error       TEXT,
    stream      INTEGER,
    host        TEXT
);
"""

_SCHEMA_INDEXES = """
-- ts 上的索引同时服务三件事：按时间倒序列表、时间范围统计、按天清理。
CREATE INDEX IF NOT EXISTS idx_traces_ts ON traces(ts DESC);
-- 只查失败记录是最常见的排查动作，单独给它一个偏索引。
CREATE INDEX IF NOT EXISTS idx_traces_failed ON traces(ts DESC) WHERE status_code < 0 OR status_code >= 400;
-- 目标站点排行按 host 分组，且总是带时间范围
CREATE INDEX IF NOT EXISTS idx_traces_host ON traces(host, ts DESC);
"""


_CLAIM_SUFFIX = ".owner"


def _claim_database(path: str) -> str:
    marker = path + _CLAIM_SUFFIX
    try:
        previous = int(Path(marker).read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        previous = 0

    if previous and previous != os.getpid() and _process_alive(previous):
        log.warning(
            f"链路记录库 {path} 正被另一个进程（pid {previous}）写入。"
            f"多个实例写同一个库不会报错，但记录会**静默混在一起**，界面上分不出来。"
            f"给 [TRACE].sqlite_path 加上 {{port}} 占位符即可按端口分开，"
            f'例如 sqlite_path = "ipclick-trace.{{port}}.db"'
        )

    try:
        _ = Path(marker).write_text(str(os.getpid()), encoding="utf-8")
    except OSError as e:
        log.debug(f"无法写入链路库占用标记 {marker}：{e}")
        return ""
    return marker


def _release_database(marker: str) -> None:
    if not marker:
        return
    with contextlib.suppress(OSError):
        if Path(marker).read_text(encoding="utf-8").strip() == str(os.getpid()):
            Path(marker).unlink()


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class TraceReader:
    def __init__(self, path: str) -> None:
        self.path: str = os.path.abspath(path)

    def exists(self) -> bool:
        return os.path.isfile(self.path)

    def _connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        if readonly:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5.0)
        else:
            conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def query(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        since: float | None = None,
        status_class: str = "",
        adapter: str = "",
        keyword: str = "",
    ) -> list[TraceRecord]:
        where: list[str] = []
        params: list[Any] = []
        if since is not None:
            where.append("ts >= ?")
            params.append(since)
        if adapter:
            where.append("adapter = ?")
            params.append(adapter)
        if keyword:
            where.append("url LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(keyword)}%")
        clause = _status_clause(status_class)
        if clause:
            where.append(clause)
        sql = "SELECT * FROM traces"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC LIMIT ? OFFSET ?"
        params.extend([max(1, limit), max(0, offset)])
        try:
            conn = self._connect(readonly=True)
            try:
                return [TraceRecord.from_row(row) for row in conn.execute(sql, params)]
            finally:
                conn.close()
        except sqlite3.Error as e:
            log.warning(f"链路记录查询失败: {e}")
            return []

    def summary(self, since: float | None = None) -> dict[str, Any]:
        where = "WHERE ts >= ?" if since is not None else ""
        params: tuple[Any, ...] = (since,) if since is not None else ()
        try:
            conn = self._connect(readonly=True)
            try:
                row = conn.execute(
                    f"""SELECT COUNT(*) AS total,
                               SUM(CASE WHEN status_code BETWEEN 200 AND 399 THEN 1 ELSE 0 END) AS ok,
                               AVG(duration_ms) AS avg_ms,
                               SUM(size) AS bytes,
                               MIN(ts) AS first_ts
                        FROM traces {where}""",
                    params,
                ).fetchone()
                by_adapter = {
                    str(r["adapter"]): {
                        "total": int(r["total"]),
                        "ok": int(r["ok"] or 0),
                        "failed": int(r["total"]) - int(r["ok"] or 0),
                        "avg_ms": round(float(r["avg_ms"] or 0), 1),
                        "bytes": int(r["bytes"] or 0),
                    }
                    for r in conn.execute(
                        f"""SELECT adapter, COUNT(*) AS total,
                                   SUM(CASE WHEN status_code BETWEEN 200 AND 399 THEN 1 ELSE 0 END) AS ok,
                                   AVG(duration_ms) AS avg_ms, SUM(size) AS bytes
                            FROM traces {where} GROUP BY adapter ORDER BY total DESC""",
                        params,
                    )
                }
                total = int(row["total"] or 0)
                ok = int(row["ok"] or 0)
                return {
                    "total": total,
                    "ok": ok,
                    "failed": total - ok,
                    "success_rate": round(ok / total * 100, 1) if total else 0.0,
                    "avg_ms": round(float(row["avg_ms"] or 0), 1),
                    "bytes": int(row["bytes"] or 0),
                    "first_ts": float(row["first_ts"]) if row["first_ts"] else None,
                    "by_adapter": by_adapter,
                }
            finally:
                conn.close()
        except sqlite3.Error as e:
            log.warning(f"链路统计失败: {e}")
            return {"total": 0, "ok": 0, "failed": 0, "success_rate": 0.0, "avg_ms": 0.0, "bytes": 0, "by_adapter": {}}

    def daily(self, days: int = 30) -> list[dict[str, Any]]:
        since = time.time() - days * 86400
        try:
            conn = self._connect(readonly=True)
            try:
                return [
                    {
                        "day": str(r["day"]),
                        "total": int(r["total"]),
                        "ok": int(r["ok"] or 0),
                        "failed": int(r["total"]) - int(r["ok"] or 0),
                        "avg_ms": round(float(r["avg_ms"] or 0), 1),
                    }
                    for r in conn.execute(
                        """SELECT date(ts, 'unixepoch', 'localtime') AS day,
                                  COUNT(*) AS total,
                                  SUM(CASE WHEN status_code BETWEEN 200 AND 399 THEN 1 ELSE 0 END) AS ok,
                                  AVG(duration_ms) AS avg_ms
                           FROM traces WHERE ts >= ? GROUP BY day ORDER BY day""",
                        (since,),
                    )
                ]
            finally:
                conn.close()
        except sqlite3.Error as e:
            log.warning(f"链路趋势统计失败: {e}")
            return []

    def top_hosts(self, since: float | None = None, limit: int = 10) -> list[dict[str, Any]]:
        where = "WHERE ts >= ?" if since is not None else ""
        params: tuple[Any, ...] = (since,) if since is not None else ()
        sql = f"""SELECT COALESCE(host, '-') AS host, COUNT(*) AS total,
                         SUM(CASE WHEN status_code BETWEEN 200 AND 399 THEN 1 ELSE 0 END) AS ok,
                         AVG(duration_ms) AS avg_ms
                  FROM traces {where}
                  GROUP BY COALESCE(host, '-') ORDER BY total DESC LIMIT ?"""
        try:
            conn = self._connect(readonly=True)
            try:
                rows = conn.execute(sql, (*params, max(1, limit))).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as e:
            log.warning(f"目标站点排行统计失败: {e}")
            return []
        return [
            {
                "host": str(r["host"] or "-"),
                "total": int(r["total"]),
                "ok": int(r["ok"] or 0),
                "failed": int(r["total"]) - int(r["ok"] or 0),
                "avg_ms": round(float(r["avg_ms"] or 0), 1),
            }
            for r in rows
        ]

    def db_size(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                total += os.path.getsize(self.path + suffix)
        return total

    def count(self) -> int:
        try:
            conn = self._connect(readonly=True)
            try:
                row = conn.execute("SELECT COUNT(*) AS n FROM traces").fetchone()
                return int(row["n"])
            finally:
                conn.close()
        except sqlite3.Error:
            return 0


@final
class SQLiteSink(TraceReader):
    def __init__(self, path: str, retention_days: int = 30, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        super().__init__(path)
        self.retention_days: int = retention_days
        self.dropped: int = 0
        self.written: int = 0
        self.failed: bool = False

        self._queue: queue.Queue[TraceRecord | None] = queue.Queue(maxsize=queue_size)
        self._lock: threading.Lock = threading.Lock()
        self._last_drop_log: float = 0.0
        self._closed: bool = False

        self._init_db()
        self._claim_marker: str = _claim_database(self.path)
        self._thread: threading.Thread = threading.Thread(target=self._run, name="ipclick-trace", daemon=True)
        self._thread.start()

    def _init_db(self) -> None:
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = self._connect()
        try:
            _ = conn.execute("PRAGMA journal_mode=WAL")
            _ = conn.execute("PRAGMA synchronous=NORMAL")
            _ = conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            _ = conn.executescript(_SCHEMA_TABLE)
            self._migrate(conn)
            _ = conn.executescript(_SCHEMA_INDEXES)
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        existing = {str(row["name"]) for row in conn.execute("PRAGMA table_info(traces)")}
        if "host" not in existing:
            _ = conn.execute("ALTER TABLE traces ADD COLUMN host TEXT")
            _ = conn.execute(
                "UPDATE traces SET host = CASE"
                "  WHEN instr(url, '://') = 0 THEN COALESCE(NULLIF(url, ''), '-')"
                "  WHEN instr(substr(url, instr(url, '://') + 3), '/') = 0"
                "    THEN substr(url, instr(url, '://') + 3)"
                "  ELSE substr(url, instr(url, '://') + 3,"
                "              instr(substr(url, instr(url, '://') + 3), '/') - 1)"
                " END WHERE host IS NULL"
            )
            log.info("链路记录库已补上 host 列（用于目标站点排行）")

    def submit(self, record: TraceRecord) -> None:
        if self._closed or self.failed:
            return
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            with self._lock:
                self.dropped += 1
                dropped = self.dropped
                now = time.monotonic()
                should_log = now - self._last_drop_log > 60
                if should_log:
                    self._last_drop_log = now
            if should_log:
                log.warning(
                    f"链路记录队列已满，累计丢弃 {dropped} 条（写盘跟不上请求速率；可关闭 [TRACE].sqlite_enabled）"
                )

    def _run(self) -> None:
        next_retention = 0.0
        while True:
            batch = self._drain()
            if batch is None:
                break
            if batch:
                self._write(batch)
            if self.failed:
                break
            now = time.monotonic()
            if now >= next_retention:
                next_retention = now + _RETENTION_INTERVAL
                self._purge()

    def _drain(self) -> list[TraceRecord] | None:
        batch: list[TraceRecord] = []
        try:
            first = self._queue.get(timeout=_FLUSH_INTERVAL)
        except queue.Empty:
            return batch
        if first is None:
            return None
        batch.append(first)

        deadline = time.monotonic() + _FLUSH_INTERVAL
        while len(batch) < _BATCH_SIZE:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                item = self._queue.get(timeout=remaining)
            except queue.Empty:
                break
            if item is None:
                if batch:
                    self._write(batch)
                return None
            batch.append(item)
        return batch

    def _write(self, batch: Sequence[TraceRecord]) -> None:
        try:
            conn = self._connect()
            try:
                _ = conn.executemany(_INSERT_SQL, [r.as_row() for r in batch])
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as e:
            self.failed = True
            log.error(f"链路记录写入失败，已停止落盘（内存记录不受影响）: {e}")
            return
        with self._lock:
            self.written += len(batch)

    def _purge(self) -> None:
        if self.retention_days <= 0:
            return
        cutoff = time.time() - self.retention_days * 86400
        try:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM traces WHERE ts < ?", (cutoff,))
                removed = cur.rowcount
                conn.commit()
                if removed > 0:
                    _ = conn.execute("PRAGMA incremental_vacuum(1000)")
                    conn.commit()
                    log.info(f"链路记录清理：删除 {removed} 条超过 {self.retention_days} 天的记录")
            finally:
                conn.close()
        except sqlite3.Error as e:
            log.warning(f"链路记录清理失败: {e}")

    def close(self, timeout: float = 5.0) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)
        self._thread.join(timeout=timeout)
        _release_database(self._claim_marker)


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _status_clause(status_class: str) -> str:
    return {
        "2xx": "status_code BETWEEN 200 AND 299",
        "3xx": "status_code BETWEEN 300 AND 399",
        "4xx": "status_code BETWEEN 400 AND 499",
        "5xx": "status_code >= 500",
        "failure": "status_code < 0",
        "failed": "status_code < 0 OR status_code >= 400",
    }.get(status_class, "")


@final
@dataclass
class RequestTrace:
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
    def __init__(self, settings: TraceSettings | None = None) -> None:
        self.settings: TraceSettings = settings or TraceSettings()
        self.node_id: str = self.settings.node_id or _default_node_id()
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
        tr = RequestTrace(adapter=adapter, method=method, uuid=uuid, url=url, stream=stream, node_id=self.node_id)
        self.counters.enter()
        start = time.monotonic()
        try:
            yield tr
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            self.emit(tr, duration_ms)

    def emit(self, tr: RequestTrace, duration_ms: int) -> None:
        url = tr.url if self.settings.record_url else _host_only(tr.url)
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
            error=tr.error,
            stream=tr.stream,
        )
        self.counters.leave(record)
        self._push(record)

    def _push(self, record: TraceRecord) -> None:
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
        self.counters.record_retry(f"{adapter}:{reason}" if adapter else reason)

    def record_rejected(self, reason: str) -> None:
        self.counters.record_rejected(reason)

    def recent(
        self,
        limit: int = 100,
        *,
        status_class: str = "",
        adapter: str = "",
        keyword: str = "",
    ) -> list[TraceRecord]:
        with self._recent_lock:
            records = list(reversed(self._recent))
        return [r for r in records if _matches(r, status_class, adapter, keyword)][: max(1, limit)]

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
        out: dict[str, Any] = {"process": self.counters.snapshot(), "recorder": self.status()}
        if self.sink is not None and not self.sink.failed:
            out.update(self._window_stats(days))
        return out

    def _window_stats(self, days: int) -> dict[str, Any]:
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
        if self.sink is not None:
            self.sink.close()


def _matches(record: TraceRecord, status_class: str, adapter: str, keyword: str) -> bool:
    if adapter and record.adapter != adapter:
        return False
    if keyword and keyword.lower() not in record.url.lower():
        return False
    if not status_class:
        return True
    if status_class == "failed":
        return not record.ok
    return record.status_class == status_class


def _host_only(url: str) -> str:
    from ipclick.limiter import host_of

    return host_of(url) or ""


def _default_node_id() -> str:
    import socket

    try:
        host = socket.gethostname()
    except OSError:
        host = "unknown"
    return f"{host}:{os.getpid()}"


_recorder: TraceRecorder | None = None
_recorder_lock = threading.Lock()


def get_recorder() -> TraceRecorder:
    global _recorder
    if _recorder is not None:
        return _recorder
    with _recorder_lock:
        if _recorder is None:
            _recorder = TraceRecorder()
    return _recorder


def init_recorder(settings: TraceSettings) -> TraceRecorder:
    global _recorder
    with _recorder_lock:
        old = _recorder
        _recorder = TraceRecorder(settings)
    if old is not None:
        old.close()
    return _recorder


def reset_recorder() -> None:
    global _recorder
    with _recorder_lock:
        old = _recorder
        _recorder = None
    if old is not None:
        old.close()


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
