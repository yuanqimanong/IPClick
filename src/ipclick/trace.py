"""请求链路记录与统计。

这个模块替代了原来的 Prometheus 埋点。取舍是刻意的：Prometheus 能回答
"整体 QPS 和 P99 是多少"，但回答不了"我刚才那个请求为什么 403"——因为它按
设计不保留单条记录（标签里放 URL 会造成基数爆炸）。而这个库的使用场景恰恰
是后者，所以这里换成两层结构：

1. **内存环形缓冲**（始终开启，默认 500 条）。零依赖、零磁盘、上限固定。
   进程重启即丢——它服务的是"刚才发生了什么"。
2. **SQLite**（``[TRACE].sqlite_enabled``，**默认关**）。按天保留，默认 30 天。
   服务的是"上周三那批任务的成功率"。

聚合统计（总数 / 成功率 / 各适配器耗时）用进程内计数器实时累加，即使
SQLite 关着 Web 端也有数可看——代价只是重启归零。

写 SQLite 的三条硬约束：

* **单写线程 + 有界队列**。SQLite 同一时刻只允许一个写者；让 N 个 gRPC
  worker 各自去写等于在热路径上抢锁。请求线程只做一次 ``put_nowait``。
* **队列满了就丢，并且丢了要能看见**。链路日志是可观测性数据，绝不能反压
  业务——但静默丢弃比丢弃更糟，所以有 ``dropped`` 计数器并会打日志。
* **写失败不能传播**。磁盘满、文件被删、权限变更都不该让一次正常的下载失败。
"""

from __future__ import annotations

from collections.abc import Generator, Sequence
import contextlib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import os
import queue
import sqlite3
import threading
import time
from typing import Any, final

from ipclick.utils.log_util import log


#: 内存环形缓冲的默认容量。500 条 × 约 400 字节 ≈ 200KB，可以忽略。
DEFAULT_MEMORY_SIZE = 500

#: SQLite 写队列容量。按 5000 条 / 批量 200 算，够扛十几秒的写入尖峰。
DEFAULT_QUEUE_SIZE = 5000

#: 一次事务最多攒多少条。攒批是 SQLite 写入吞吐的关键（逐条 commit 会慢两个数量级）。
_BATCH_SIZE = 200

#: 攒批的最长等待。低流量时不能让记录一直卡在队列里看不到。
_FLUSH_INTERVAL = 0.5

#: 清理过期数据的间隔（秒）。按天保留，一小时一次足够。
_RETENTION_INTERVAL = 3600.0

#: 跨天聚合的缓存有效期（秒）。请求流页每 3 秒刷新一次，而这些数字描述的是
#: 一个 30 天的窗口——"3 秒新"没有意义，重复算却很贵。
WINDOW_CACHE_TTL = 10.0

#: URL 截断长度。爬虫的 URL 可能带很长的查询串，全存进去会让库膨胀得毫无必要。
_URL_MAX_LEN = 512


def classify_status(status_code: int) -> str:
    """把状态码归成有限的几类。"""
    if status_code < 0:
        return "failure"  # 连接层失败，没拿到 HTTP 响应
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
    """一条请求链路记录。

    刻意**不含**请求头、cookie、请求体、代理串——那些里面有机密，而这张表是
    要给 Web 端展示的。想看完整请求内容请开 debug 日志。
    """

    ts: float
    """完成时刻（unix 秒）。"""
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
        """目标站点。用 :func:`ipclick.limiter.host_of`——和按 host 限流是同一套
        定义，两处对"host"的理解必须一致，否则排行榜和限流说的不是一回事。

        ``[TRACE].record_url = false`` 时 :attr:`url` 里存的**已经是** host
        （不带协议），而 ``host_of`` 要的是完整 URL，对它会解析失败。
        所以解析不出来时把 url 原样当 host 用——否则关掉 record_url 的部署里
        整个目标站点排行会全是 "-"。
        """
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
        """本地时间字符串，给 Web 端直接用。"""
        return datetime.fromtimestamp(self.ts).strftime("%Y-%m-%d %H:%M:%S")

    def as_row(self) -> tuple[Any, ...]:
        """转成 SQLite 的插入元组（列顺序见 _INSERT_SQL）。"""
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
    """``[TRACE]`` 配置。"""

    memory_size: int = DEFAULT_MEMORY_SIZE
    sqlite_enabled: bool = False
    sqlite_path: str = "ipclick-trace.db"
    retention_days: int = 30
    #: 只记失败的请求。成功量极大又只关心异常时打开，能把库缩小一两个数量级。
    only_errors: bool = False
    queue_size: int = DEFAULT_QUEUE_SIZE
    #: 记录 URL。默认记——查问题基本都要它。介意的话可以关，关掉后只留 host。
    record_url: bool = True
    node_id: str = ""

    @classmethod
    def from_config(cls, section: dict[str, Any], node_id: str = "") -> TraceSettings:
        defaults = cls()

        def _int(key: str, default: int, minimum: int) -> int:
            try:
                return max(minimum, int(section.get(key, default)))
            except (TypeError, ValueError):
                log.warning(f"[TRACE].{key} 不是整数，改用默认值 {default}")
                return default

        return cls(
            memory_size=_int("memory_size", defaults.memory_size, 0),
            sqlite_enabled=bool(section.get("sqlite_enabled", defaults.sqlite_enabled)),
            sqlite_path=str(section.get("sqlite_path", defaults.sqlite_path) or defaults.sqlite_path),
            # 0 表示永久保留
            retention_days=_int("retention_days", defaults.retention_days, 0),
            only_errors=bool(section.get("only_errors", defaults.only_errors)),
            queue_size=_int("queue_size", defaults.queue_size, 100),
            record_url=bool(section.get("record_url", defaults.record_url)),
            node_id=node_id or str(section.get("node_id", "") or ""),
        )


# --------------------------------------------------------------------------- #
# 聚合计数器
# --------------------------------------------------------------------------- #


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
    """进程内聚合计数器。始终开启，成本是几次整数自增。"""

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


# --------------------------------------------------------------------------- #
# SQLite 落盘
# --------------------------------------------------------------------------- #

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
    # 单独存一列而不是查询时从 url 现算：SQL 里没有 URL 解析函数，自己用
    # instr/substr 拼出来的那套和 host_of 对不齐（端口、IPv6 字面量），
    # 于是"排行榜里的 host"和"限流用的 host"会是两个东西。
    "host",
)

_INSERT_SQL = f"INSERT INTO traces ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' * len(_COLUMNS))})"

#: 建表。索引单独放在 _SCHEMA_INDEXES —— 它们可能引用后加的列，
#: 必须等迁移补完列之后再建，否则在老库上会 "no such column"。
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


@final
class SQLiteSink:
    """把链路记录异步写进 SQLite。

    ``close()`` 之前所有写入都只经过一个后台线程，因此不需要跨线程共享连接。
    读取（Web 端查询）每次开一条新连接——sqlite3 的连接不是线程安全的，
    而 WAL 模式下读不阻塞写。
    """

    def __init__(self, path: str, retention_days: int = 30, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        self.path: str = os.path.abspath(path)
        self.retention_days: int = retention_days
        self.dropped: int = 0
        self.written: int = 0
        self.failed: bool = False

        self._queue: queue.Queue[TraceRecord | None] = queue.Queue(maxsize=queue_size)
        self._lock: threading.Lock = threading.Lock()
        self._last_drop_log: float = 0.0
        self._closed: bool = False

        self._init_db()
        self._thread: threading.Thread = threading.Thread(target=self._run, name="ipclick-trace", daemon=True)
        self._thread.start()

    # -------------------------------------------------------------- #
    # 建库
    # -------------------------------------------------------------- #

    def _connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        if readonly:
            # uri 模式的 mode=ro 能保证 Web 端的查询绝不可能写坏数据文件
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5.0)
        else:
            conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = self._connect()
        try:
            # WAL：读写互不阻塞。这是"Web 端在查的同时还能继续写"的前提。
            _ = conn.execute("PRAGMA journal_mode=WAL")
            # NORMAL：不为每次 commit 做 fsync。断电可能丢最后几条链路记录——
            # 对可观测性数据这个代价换来的吞吐提升是划算的。
            _ = conn.execute("PRAGMA synchronous=NORMAL")
            # 增量整理：按天 DELETE 之后用 incremental_vacuum 回收页面。
            # 不用 VACUUM——它要独占锁并重写整个文件，库大了会把服务卡住。
            #
            # ⚠️ 这条 PRAGMA 只对**新建**的库生效，且必须在建表之前执行（所以它
            # 在 _SCHEMA_TABLE 上面）。已经存在的库改不了 auto_vacuum 模式，
            # 语句会被静默忽略，_purge() 里的 incremental_vacuum 也就成了空操作
            # ——文件不会再缩小，但删数据、查询、保留期都照常工作。真要回收
            # 旧库的空间，只能停服后手动 VACUUM 重建。
            _ = conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            _ = conn.executescript(_SCHEMA_TABLE)
            # 先补列再建索引：索引可能引用后加的列
            self._migrate(conn)
            _ = conn.executescript(_SCHEMA_INDEXES)
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """把老库补上后加的列。

        只加列、不删列、不改已有列的类型——那类改动 SQLite 要重建整张表。

        新列的回填是个例外：``host`` 全空的话目标站点排行对历史数据就是一片
        ``-``，等于这个功能对老库不存在。回填只在**加列的那一次**跑，之后每次
        启动都只是一次 ``PRAGMA table_info``。
        """
        existing = {str(row["name"]) for row in conn.execute("PRAGMA table_info(traces)")}
        if "host" not in existing:
            _ = conn.execute("ALTER TABLE traces ADD COLUMN host TEXT")
            # 老行的 host 用 SQL 现补一次：写法和 host_of 对不齐的边角情况
            # （端口、IPv6）在这里可以接受——它只影响历史数据的排行分组，
            # 新写入的行走的是 host_of。
            # 不含 "://" 的行是 record_url = false 时写的，url 里存的**已经是**
            # host，原样用——和 TraceRecord.host 的兜底保持一致。
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

    # -------------------------------------------------------------- #
    # 写
    # -------------------------------------------------------------- #

    def submit(self, record: TraceRecord) -> None:
        """入队，绝不阻塞。队列满就丢并计数。"""
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
        next_retention = 0.0  # 启动后立即清一次
        while True:
            batch = self._drain()
            if batch is None:
                break
            if batch:
                self._write(batch)
            if self.failed:
                # 落盘已经废了（磁盘满、权限、库损坏），继续转下去只会对每一批
                # 重复同一个错误、刷满日志。submit() 也已经不再入队，这个线程
                # 没有活可干了——退出，记录降级成只有内存缓冲。
                break
            now = time.monotonic()
            if now >= next_retention:
                next_retention = now + _RETENTION_INTERVAL
                self._purge()

    def _drain(self) -> list[TraceRecord] | None:
        """攒一批。返回 None 表示收到了关闭信号。"""
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
                # 关闭信号：先把手上这批写完，再退出
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
            # 写链路日志失败绝不能影响下载。降级成"只有内存缓冲"并说清原因。
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
                    # 回收 DELETE 释放的页面。分批做，不独占锁。
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
        # 队列满时塞不进关闭信号，只能等超时——线程是 daemon，进程退出不受影响
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)
        self._thread.join(timeout=timeout)

    # -------------------------------------------------------------- #
    # 读（给 Web 端）
    # -------------------------------------------------------------- #

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
        """按条件倒序查记录。参数全部走占位符，不做字符串拼接。"""
        where: list[str] = []
        params: list[Any] = []
        if since is not None:
            where.append("ts >= ?")
            params.append(since)
        if adapter:
            where.append("adapter = ?")
            params.append(adapter)
        if keyword:
            # LIKE 里 % 和 _ 是通配符，而 URL 里这两个字符都很常见
            # （/api_v2/、utm_source=）。不转义的话 "api_v2" 会匹配到 "apiXv2"，
            # 用户以为筛出来的是精确子串，实际不是。
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
        """时间范围内的聚合。SQL 里算，不把行拉到 Python 侧。"""
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
        """按天分组的计数，给 Web 端画趋势。"""
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
                    # unixepoch + localtime：按本地日期分组，否则跨时区看起来会错一天
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
        """按目标 host 排行。

        host 是入库时就算好的一列（见 :data:`_COLUMNS`），所以这里是一次带索引的
        GROUP BY。原实现是 ``query(limit=20000)`` 把两万行拉进 Python 再聚合——
        既慢（实测 219ms），那个 20000 的上限还是**静默**的：超过之后排行只反映
        其中一部分，页面上却看不出来。
        """
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
        """数据文件大小（含 WAL）。"""
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


def _escape_like(text: str) -> str:
    """转义 LIKE 模式里的通配符。

    反斜杠自己要先转，否则它会把后面那个转义序列吃掉。
    配套的 SQL 要写 ``ESCAPE '\\'``。
    """
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _status_clause(status_class: str) -> str:
    """把 "2xx" / "failure" 这类分类翻成固定的 SQL 片段。

    返回的是写死的常量串，不含外部输入——不认识的分类返回空串（不过滤），
    避免把用户输入拼进 SQL。
    """
    return {
        "2xx": "status_code BETWEEN 200 AND 299",
        "3xx": "status_code BETWEEN 300 AND 399",
        "4xx": "status_code BETWEEN 400 AND 499",
        "5xx": "status_code >= 500",
        "failure": "status_code < 0",
        "failed": "status_code < 0 OR status_code >= 400",
    }.get(status_class, "")


# --------------------------------------------------------------------------- #
# 记录器
# --------------------------------------------------------------------------- #


@final
@dataclass
class RequestTrace:
    """一次请求处理过程中攒出来的链路信息。

    在 ``track_request`` 里创建，执行过程中被逐步填充，退出时定型成
    :class:`TraceRecord`。用可变对象而不是原来那个 ``dict``——字段名写错时
    类型检查能当场发现，而 ``ctx["stauts_code"] = 200`` 会静默无效。
    """

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
    """链路记录的统一入口。进程级单例，见 :func:`get_recorder`。"""

    def __init__(self, settings: TraceSettings | None = None) -> None:
        self.settings: TraceSettings = settings or TraceSettings()
        self.node_id: str = self.settings.node_id or _default_node_id()
        self.counters: Counters = Counters()
        self._recent: list[TraceRecord] = []
        self._recent_lock: threading.Lock = threading.Lock()
        #: 跨天聚合的短 TTL 缓存，键是天数。见 stats()。
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
                # 建不了库就退回纯内存，服务照常起。
                log.error(f"链路记录落盘启用失败，退回内存模式: {e}")
                self.sink = None

    # -------------------------------------------------------------- #
    # 埋点
    # -------------------------------------------------------------- #

    @contextmanager
    def track_request(
        self, adapter: str, method: str, *, uuid: str = "", url: str = "", stream: bool = False
    ) -> Generator[RequestTrace]:
        """包住一次请求处理，退出时落一条记录。

        用法::

            with recorder.track_request("curl_cffi", "GET", url=url) as tr:
                response = do_work()
                tr.status_code = response.status_code
                tr.size = len(response.content or b"")
        """
        tr = RequestTrace(adapter=adapter, method=method, uuid=uuid, url=url, stream=stream, node_id=self.node_id)
        self.counters.enter()
        start = time.monotonic()
        try:
            yield tr
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            self.emit(tr, duration_ms)

    def emit(self, tr: RequestTrace, duration_ms: int) -> None:
        """把一次请求的结果落成记录。``track_request`` 会自动调用。"""
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
                    # 一次多丢一点，避免每条都做一次列表搬移
                    overflow = len(self._recent) - self.settings.memory_size
                    del self._recent[:overflow]
        if self.sink is not None:
            self.sink.submit(record)

    def record_retry(self, adapter: str, reason: str) -> None:
        self.counters.record_retry(f"{adapter}:{reason}" if adapter else reason)

    def record_rejected(self, reason: str) -> None:
        self.counters.record_rejected(reason)

    # -------------------------------------------------------------- #
    # 查询（给 Web 端）
    # -------------------------------------------------------------- #

    def recent(
        self,
        limit: int = 100,
        *,
        status_class: str = "",
        adapter: str = "",
        keyword: str = "",
    ) -> list[TraceRecord]:
        """内存里的最近记录（倒序）。SQLite 关着时 Web 端就靠这个。"""
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
        """统一查询入口。返回 (记录, 数据来源)——来源要给用户看见，
        否则"只有 200 条"和"30 天只有 200 条"分不清。
        """
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
        """记录器自身的状态，给 Web 端显示"数据从哪来、丢了多少"。"""
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
        """给 Web 端的一整份统计。

        进程计数器（实时）+ 时间范围聚合与按天趋势（带短 TTL 缓存）。

        缓存只盖住跨天聚合那几项：它们描述的是一个 30 天的窗口，"3 秒新"这件事
        毫无意义，而请求流页恰恰每 3 秒刷新一次。实测 20 万行时这三个查询合计
        接近 1 秒，不缓存的话三个节点光算看板就要吃掉一整个核。
        在途数、总数这些实时值不走缓存。
        """
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
        if sink is None:  # pragma: no cover - 调用方已经判过
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
    """没配 node_id 时的兜底：主机名 + pid。

    带上 pid 是因为同一台机器上跑多个实例（不同端口）时，光有主机名分不出来。
    """
    import socket

    try:
        host = socket.gethostname()
    except OSError:  # pragma: no cover
        host = "unknown"
    return f"{host}:{os.getpid()}"


#: 进程级单例。埋点处直接用，不必层层传递。
_recorder: TraceRecorder | None = None
_recorder_lock = threading.Lock()


def get_recorder() -> TraceRecorder:
    """取进程级记录器（首次调用时按默认配置创建）。

    默认配置只有内存缓冲，不碰磁盘——所以在库模式下用也没有副作用。
    """
    global _recorder
    if _recorder is not None:
        return _recorder
    with _recorder_lock:
        if _recorder is None:
            _recorder = TraceRecorder()
    return _recorder


def init_recorder(settings: TraceSettings) -> TraceRecorder:
    """按配置（重新）初始化单例。服务端启动时调用。"""
    global _recorder
    with _recorder_lock:
        old = _recorder
        _recorder = TraceRecorder(settings)
    if old is not None:
        old.close()
    return _recorder


def reset_recorder() -> None:
    """重置单例。测试隔离用。"""
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
    "TraceRecord",
    "TraceRecorder",
    "TraceSettings",
    "classify_status",
    "get_recorder",
    "init_recorder",
    "reset_recorder",
]
