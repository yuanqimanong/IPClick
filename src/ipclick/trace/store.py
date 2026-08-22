"""SQLite 存储引擎：建表与迁移、异步写入、保留期清理、查询与统计。

也包含"哪个进程拥有这个库文件"的认领协议（_claim_database）——多实例指向同一个
sqlite_path 时靠它给出明确告警，而不是让两个进程静默互相覆盖。
"""

from __future__ import annotations

from collections.abc import Sequence
import contextlib
import os
from pathlib import Path
import queue
import sqlite3
import sys
import threading
import time
from typing import Any, final
from urllib.parse import quote

from ipclick.trace.records import DEFAULT_QUEUE_SIZE, TraceRecord
from ipclick.utils.log_util import log


_BATCH_SIZE = 200

_FLUSH_INTERVAL = 0.5

# 超过这个大小就不做启动时的一次性 VACUUM 转换，避免阻塞启动。
_VACUUM_CONVERT_LIMIT = 256 * 1024 * 1024

_RETENTION_INTERVAL = 3600.0


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
CREATE INDEX IF NOT EXISTS idx_traces_failed ON traces(ts DESC) WHERE status_code < 200 OR status_code >= 400;
-- 目标站点排行按 host 分组，且总是带时间范围
CREATE INDEX IF NOT EXISTS idx_traces_host ON traces(host, ts DESC);
"""


_CLAIM_SUFFIX = ".owner"


def _claim_database(path: str) -> str:
    """写入进程占用标记，并提示多个实例误用同一链路库。"""
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
    """仅由仍持有标记的进程清理占用文件。"""
    if not marker:
        return
    with contextlib.suppress(OSError):
        if Path(marker).read_text(encoding="utf-8").strip() == str(os.getpid()):
            Path(marker).unlink()


def _process_alive(pid: int) -> bool:
    """尽力判断 POSIX 进程是否存活；Windows 采用保守结果。"""
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
    """以短连接方式只读查询 SQLite 链路库。"""

    def __init__(self, path: str) -> None:
        """保存规范化后的数据库绝对路径。"""
        self.path: str = os.path.abspath(path)

    def exists(self) -> bool:
        """数据库文件是否存在。"""
        return os.path.isfile(self.path)

    def _connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        """打开带 ``sqlite3.Row`` 行工厂的短生命周期连接。"""
        if readonly:
            # SQLite URI 会把 ? 和 # 当作查询串/片段；路径必须先编码，
            # 同时保留盘符冒号和目录分隔符。
            encoded_path = quote(Path(self.path).as_posix(), safe="/:")
            conn = sqlite3.connect(f"file:{encoded_path}?mode=ro", uri=True, timeout=5.0)
        else:
            conn = sqlite3.connect(self.path, timeout=10.0)
            # synchronous 是**按连接**生效的，不像 journal_mode / auto_vacuum 那样写进
            # 文件头。原来只在 _init_db 那条连接上设过，而它随即就关了：此后每次 _write /
            # _purge 新开的连接都退回默认的 FULL，于是每 0.5 秒一次的批量提交都要整盘
            # fsync——正是这条 pragma 想省掉的开销，队列被写满、记录被丢弃就更容易发生。
            _ = conn.execute("PRAGMA synchronous=NORMAL")
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
        """按时间倒序查询链路，并支持状态、适配器和 URL 过滤。"""
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
        """汇总指定时间窗内的总体和各适配器指标。"""
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
        """按服务端本地日期汇总最近若干天的趋势。"""
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
        """返回指定时间窗内请求量最高的目标主机。"""
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
        """计算主库、WAL 与共享内存文件的总占用。"""
        total = 0
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                total += os.path.getsize(self.path + suffix)
        return total

    def count(self) -> int:
        """返回链路总行数，数据库不可读时返回零。"""
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
    """通过有界队列和单独线程批量写入链路数据库。"""

    def __init__(self, path: str, retention_days: int = 30, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        """初始化数据库、占用标记及后台写线程。"""
        super().__init__(path)
        self.retention_days: int = retention_days
        self.dropped: int = 0
        self.written: int = 0
        self.failed: bool = False

        self._queue: queue.Queue[TraceRecord | None] = queue.Queue(maxsize=queue_size)
        self._lock: threading.Lock = threading.Lock()
        self._last_drop_log: float = 0.0
        self._closed: bool = False
        self._stop: threading.Event = threading.Event()

        self._init_db()
        self._claim_marker: str = _claim_database(self.path)
        self._thread: threading.Thread = threading.Thread(target=self._run, name="ipclick-trace", daemon=True)
        self._thread.start()

    def _init_db(self) -> None:
        """配置 WAL、创建表与索引，并执行兼容迁移。"""
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = self._connect()
        try:
            # auto_vacuum 必须在数据库文件被初始化**之前**设置：journal_mode=WAL 已经
            # 把文件建起来了，之后再设 auto_vacuum 会被静默忽略（读回来是 0），于是
            # _purge() 里那句 incremental_vacuum 永远是空操作——保留期删了记录，文件
            # 却永远停在历史最高水位（实测删空 20000 条后文件仍是 21 MB，freelist 5257 页）。
            _ = conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            _ = conn.execute("PRAGMA journal_mode=WAL")
            _ = conn.execute("PRAGMA synchronous=NORMAL")
            self._ensure_incremental_vacuum(conn)
            _ = conn.executescript(_SCHEMA_TABLE)
            self._migrate(conn)
            _ = conn.executescript(_SCHEMA_INDEXES)
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _ensure_incremental_vacuum(conn: sqlite3.Connection) -> None:
        """已经建好的库如果 auto_vacuum 还是 0，用一次 VACUUM 把它转过来。

        auto_vacuum 只能在库为空时直接设置，对已有数据的库必须跟一次完整 VACUUM
        才会生效。不转的话 incremental_vacuum 永远回收不到空间。
        """
        try:
            row = conn.execute("PRAGMA auto_vacuum").fetchone()
            current = int(row[0]) if row else 0
            if current == 2:
                return
            page_count = int((conn.execute("PRAGMA page_count").fetchone() or (0,))[0])
            page_size = int((conn.execute("PRAGMA page_size").fetchone() or (0,))[0])
            size = page_count * page_size
            if size > _VACUUM_CONVERT_LIMIT:
                log.warning(
                    f"链路记录库 {size / 1048576:.0f} MB 且未开启 auto_vacuum，跳过一次性转换"
                    f"（会阻塞启动）。保留期清理不会缩小文件，需要时请手工执行 VACUUM"
                )
                return
            _ = conn.execute("VACUUM")
            log.info("链路记录库已转为 auto_vacuum=INCREMENTAL，保留期清理从此可回收空间")
        except sqlite3.Error as e:
            log.debug(f"转换链路记录库的 auto_vacuum 失败（不影响写入）：{e}")

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """为旧版数据库补齐可派生的新列。"""
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
        """无阻塞提交记录；队列过载时丢弃并限频告警。"""
        should_log = False
        dropped = 0
        with self._lock:
            if self._closed:
                return
            if self.failed:
                # 已经退化到内存模式：这条记录确实被扔了，必须计入 dropped。
                # 不计的话面板上"丢弃 0 条"和"100% 没落盘"同时成立，看不出问题。
                self.dropped += 1
                return
            try:
                self._queue.put_nowait(record)
            except queue.Full:
                self.dropped += 1
                dropped = self.dropped
                now = time.monotonic()
                should_log = now - self._last_drop_log > 60
                if should_log:
                    self._last_drop_log = now
        if should_log:
            log.warning(f"链路记录队列已满，累计丢弃 {dropped} 条（写盘跟不上请求速率；可关闭 [TRACE].sqlite_enabled）")

    def _run(self) -> None:
        """持续批量落盘，并按低频周期执行保留期清理。"""
        next_retention = 0.0
        try:
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
        finally:
            # 只有真正停机（收到哨兵）才摘占用标记。因 failed 提前 break 时进程还活着、
            # 还在用这个库，摘掉标记会让后启动的实例以为没人占用，多实例告警就此失效。
            if self._closed:
                _release_database(self._claim_marker)

    def _drain(self) -> list[TraceRecord] | None:
        """等待首条记录后，在时间和条数上限内拼出一个批次。"""
        batch: list[TraceRecord] = []
        if self._stop.is_set() and self._queue.empty():
            return None
        try:
            first = self._queue.get(timeout=_FLUSH_INTERVAL)
        except queue.Empty:
            return None if self._stop.is_set() else batch
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
        """在单个事务中写入一批记录；失败后永久降级为内存模式。"""
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
        """删除超过保留期的记录，并渐进回收空闲页。"""
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
        """停止接收新记录，尽量排空队列并等待后台线程结束。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
        # 哨兵负责快速唤醒空队列；队列满时 stop event 会在排空后让 worker 自行退出。
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)
        self._thread.join(timeout=max(0.0, timeout))
        if not self._thread.is_alive():
            _release_database(self._claim_marker)
        else:
            log.warning("链路记录后台线程未在关闭超时内结束，将在排空当前队列后自行退出")


def _escape_like(text: str) -> str:
    """转义 SQLite LIKE 查询中的通配符。"""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _status_clause(status_class: str) -> str:
    """把受控状态筛选值映射为无用户插值的 SQL 条件。"""
    return {
        "2xx": "status_code BETWEEN 200 AND 299",
        "3xx": "status_code BETWEEN 300 AND 399",
        "4xx": "status_code BETWEEN 400 AND 499",
        "5xx": "status_code >= 500",
        "failure": "status_code < 200",
        "failed": "status_code < 200 OR status_code >= 400",
        "error": "status_code < 200 OR status_code >= 400",
    }.get(status_class, "")
