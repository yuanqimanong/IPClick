"""链路记录：内存缓冲、SQLite 落盘、保留期、丢弃计数。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import sqlite3
import time

import pytest

from ipclick.trace import (
    SQLiteSink,
    TraceRecord,
    TraceRecorder,
    TraceSettings,
    classify_status,
)


def _record(**kwargs: object) -> TraceRecord:
    base: dict[str, object] = {
        "ts": time.time(),
        "uuid": "u1",
        "node_id": "n1",
        "adapter": "curl_cffi",
        "method": "GET",
        "url": "http://example.com/a",
        "status_code": 200,
        "duration_ms": 12,
        "size": 100,
    }
    base.update(kwargs)
    return TraceRecord(**base)  # pyright: ignore[reportArgumentType]


class TestClassify:
    @pytest.mark.parametrize(
        ("code", "expected"),
        [(-1, "failure"), (200, "2xx"), (301, "3xx"), (404, "4xx"), (503, "5xx")],
    )
    def test_buckets(self, code: int, expected: str):
        assert classify_status(code) == expected

    def test_negative_is_not_success(self):
        assert _record(status_code=-1).ok is False


class TestMemoryBuffer:
    def test_records_are_returned_newest_first(self):
        recorder = TraceRecorder(TraceSettings(memory_size=10))
        for i in range(3):
            recorder.emit(_trace(recorder, url=f"http://example.com/{i}"), 5)
        urls = [r.url for r in recorder.recent()]
        assert urls == ["http://example.com/2", "http://example.com/1", "http://example.com/0"]

    def test_buffer_is_bounded(self):
        """环形缓冲必须有上限——爬虫跑一夜的量足够把内存吃光。"""
        recorder = TraceRecorder(TraceSettings(memory_size=5))
        for i in range(50):
            recorder.emit(_trace(recorder, url=f"http://example.com/{i}"), 1)
        recent = recorder.recent(limit=100)
        assert len(recent) == 5
        assert recent[0].url == "http://example.com/49"

    def test_memory_can_be_disabled(self):
        recorder = TraceRecorder(TraceSettings(memory_size=0))
        recorder.emit(_trace(recorder), 1)
        assert recorder.recent() == []
        # 但聚合计数照常
        assert recorder.counters.snapshot()["total"] == 1

    def test_only_errors_filters_success(self):
        recorder = TraceRecorder(TraceSettings(memory_size=10, only_errors=True))
        recorder.emit(_trace(recorder, status_code=200), 1)
        recorder.emit(_trace(recorder, status_code=500), 1)
        recorder.emit(_trace(recorder, status_code=-1), 1)
        assert [r.status_code for r in recorder.recent()] == [-1, 500]

    def test_filters(self):
        recorder = TraceRecorder(TraceSettings(memory_size=20))
        recorder.emit(_trace(recorder, status_code=404, url="http://a.com/x"), 1)
        recorder.emit(_trace(recorder, status_code=200, url="http://b.com/y", adapter="niquests"), 1)
        assert len(recorder.recent(status_class="4xx")) == 1
        assert len(recorder.recent(adapter="niquests")) == 1
        assert len(recorder.recent(keyword="b.com")) == 1
        assert len(recorder.recent(status_class="failed")) == 1

    def test_record_url_off_keeps_only_host(self):
        recorder = TraceRecorder(TraceSettings(memory_size=5, record_url=False))
        recorder.emit(_trace(recorder, url="http://example.com/secret/path?token=x"), 1)
        assert recorder.recent()[0].url == "example.com"


def _trace(recorder: TraceRecorder, **kwargs: object):
    from ipclick.trace import RequestTrace

    tr = RequestTrace(adapter="curl_cffi", method="GET", node_id=recorder.node_id, status_code=200)
    for key, value in kwargs.items():
        setattr(tr, key, value)
    return tr


class TestTrackRequest:
    def test_duration_and_counters(self):
        recorder = TraceRecorder(TraceSettings(memory_size=5))
        with recorder.track_request("curl_cffi", "GET", url="http://a.com") as tr:
            tr.status_code = 200
            tr.size = 42
        stats = recorder.counters.snapshot()
        assert stats["total"] == 1
        assert stats["ok"] == 1
        assert stats["bytes"] == 42
        assert recorder.recent()[0].duration_ms >= 0

    def test_exception_still_records(self):
        """请求处理里抛异常也要留下记录——那正是最需要查的一条。"""
        recorder = TraceRecorder(TraceSettings(memory_size=5))
        with pytest.raises(RuntimeError), recorder.track_request("curl_cffi", "GET", url="http://a.com") as tr:
            tr.status_code = -1
            raise RuntimeError("boom")
        assert len(recorder.recent()) == 1
        assert recorder.counters.snapshot()["failed"] == 1

    def test_in_flight_returns_to_zero(self):
        recorder = TraceRecorder(TraceSettings(memory_size=5))
        with recorder.track_request("curl_cffi", "GET") as tr:
            assert recorder.counters.snapshot()["in_flight"] == 1
            tr.status_code = 200
        assert recorder.counters.snapshot()["in_flight"] == 0
        assert recorder.counters.snapshot()["peak_in_flight"] == 1


@pytest.fixture
def sink(tmp_path: Path) -> Iterator[SQLiteSink]:
    s = SQLiteSink(str(tmp_path / "trace.db"), retention_days=30)
    try:
        yield s
    finally:
        s.close()


def _drain(sink: SQLiteSink, expected: int, timeout: float = 5.0) -> None:
    """等后台写线程把队列清空。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sink.written >= expected:
            return
        time.sleep(0.05)
    raise AssertionError(f"只写入了 {sink.written} 条，期望 {expected}")


class TestSQLiteSink:
    def test_write_and_query(self, sink: SQLiteSink):
        for i in range(5):
            sink.submit(_record(url=f"http://example.com/{i}", status_code=200 + i))
        _drain(sink, 5)
        rows = sink.query(limit=10)
        assert len(rows) == 5
        assert rows[0].url.startswith("http://example.com/")

    def test_binary_safe_error_text(self, sink: SQLiteSink):
        sink.submit(_record(status_code=-1, error="连接超时"))
        _drain(sink, 1)
        assert sink.query()[0].error == "连接超时"

    def test_wal_mode_enabled(self, sink: SQLiteSink):
        """WAL 是"边写边查"的前提，不能被静默降级。"""
        conn = sqlite3.connect(sink.path)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        assert str(mode).lower() == "wal"

    def test_summary_aggregates_in_sql(self, sink: SQLiteSink):
        for code in (200, 200, 500, 404):
            sink.submit(_record(status_code=code, duration_ms=10, size=5))
        _drain(sink, 4)
        summary = sink.summary()
        assert summary["total"] == 4
        assert summary["ok"] == 2
        assert summary["failed"] == 2
        assert summary["success_rate"] == 50.0
        assert summary["bytes"] == 20

    def test_summary_since_window(self, tmp_path: Path):
        # retention_days=0（永久保留），否则 40 天前那条会被启动时的清理删掉
        s = SQLiteSink(str(tmp_path / "w.db"), retention_days=0)
        try:
            s.submit(_record(ts=time.time() - 40 * 86400))
            s.submit(_record())
            _drain(s, 2)
            assert s.summary()["total"] == 2
            assert s.summary(since=time.time() - 30 * 86400)["total"] == 1
        finally:
            s.close()

    def test_daily_grouping(self, sink: SQLiteSink):
        sink.submit(_record(ts=time.time() - 86400))
        sink.submit(_record())
        sink.submit(_record())
        _drain(sink, 3)
        daily = sink.daily(days=30)
        assert len(daily) == 2
        assert sum(d["total"] for d in daily) == 3

    def test_by_adapter_breakdown(self, sink: SQLiteSink):
        sink.submit(_record(adapter="curl_cffi"))
        sink.submit(_record(adapter="niquests", status_code=500))
        _drain(sink, 2)
        by_adapter = sink.summary()["by_adapter"]
        assert by_adapter["curl_cffi"]["ok"] == 1
        assert by_adapter["niquests"]["failed"] == 1

    def test_top_hosts(self, sink: SQLiteSink):
        for _ in range(3):
            sink.submit(_record(url="http://a.com/x"))
        sink.submit(_record(url="http://b.com/y"))
        _drain(sink, 4)
        hosts = sink.top_hosts()
        assert hosts[0]["host"] == "a.com"
        assert hosts[0]["total"] == 3

    def test_retention_deletes_old_rows(self, tmp_path: Path):
        s = SQLiteSink(str(tmp_path / "t.db"), retention_days=7)
        try:
            s.submit(_record(ts=time.time() - 30 * 86400))
            s.submit(_record())
            _drain(s, 2)
            s._purge()  # pyright: ignore[reportPrivateUsage]
            assert s.count() == 1
        finally:
            s.close()

    def test_retention_zero_keeps_everything(self, tmp_path: Path):
        s = SQLiteSink(str(tmp_path / "t.db"), retention_days=0)
        try:
            s.submit(_record(ts=time.time() - 3650 * 86400))
            _drain(s, 1)
            s._purge()  # pyright: ignore[reportPrivateUsage]
            assert s.count() == 1
        finally:
            s.close()

    def test_full_queue_drops_instead_of_blocking(self, tmp_path: Path):
        """队列满了必须丢，而且要计数——可观测性数据绝不能反压业务请求。"""
        s = SQLiteSink(str(tmp_path / "t.db"), queue_size=100)
        try:
            # 直接把队列塞满，绕过后台线程（它会一直在消费）
            s._queue.maxsize = 1  # pyright: ignore[reportPrivateUsage]
            for _ in range(500):
                s._queue.put_nowait(_record())  # pyright: ignore[reportPrivateUsage]
                break
            for _ in range(50):
                s.submit(_record())
            assert s.dropped > 0
        finally:
            s.close()

    def test_status_code_filters(self, sink: SQLiteSink):
        for code in (200, 301, 404, 500, -1):
            sink.submit(_record(status_code=code))
        _drain(sink, 5)
        assert len(sink.query(status_class="2xx")) == 1
        assert len(sink.query(status_class="4xx")) == 1
        assert len(sink.query(status_class="5xx")) == 1
        assert len(sink.query(status_class="failure")) == 1
        assert len(sink.query(status_class="failed")) == 3

    def test_keyword_filter_is_parameterized(self, sink: SQLiteSink):
        """URL 关键字走占位符，注入串只会当成普通文本。"""
        sink.submit(_record(url="http://example.com/a"))
        _drain(sink, 1)
        assert sink.query(keyword="'; DROP TABLE traces; --") == []
        assert sink.count() == 1

    def test_long_url_truncated(self, sink: SQLiteSink):
        sink.submit(_record(url="http://example.com/?q=" + "x" * 5000))
        _drain(sink, 1)
        assert len(sink.query()[0].url) <= 512

    def test_writer_thread_stops_after_a_write_failure(self, sink: SQLiteSink):
        """落盘挂了之后写线程要退出，不能对每一批重复同一个错误刷满日志。

        submit() 那边已经因为 failed 不再入队，这个线程没有活可干；继续转下去
        只会把一整队积压记录逐批送进同一个必败的写入。
        """
        batches: list[int] = []

        def failing_write(batch: object) -> None:
            # 真实的 _write 不会往外抛：它吞掉 sqlite3.Error、置 failed、然后返回
            batches.append(len(batch))  # pyright: ignore[reportArgumentType]
            sink.failed = True

        object.__setattr__(sink, "_write", failing_write)
        sink._queue.put_nowait(_record())  # pyright: ignore[reportPrivateUsage]

        deadline = time.monotonic() + 5.0
        while sink._thread.is_alive() and time.monotonic() < deadline:  # pyright: ignore[reportPrivateUsage]
            time.sleep(0.05)
        assert not sink._thread.is_alive(), "写线程该在落盘失败后退出"  # pyright: ignore[reportPrivateUsage]

        # 线程已经走了，后面再进来的记录不会被反复送进那个必败的写入
        sink._queue.put_nowait(_record())  # pyright: ignore[reportPrivateUsage]
        time.sleep(0.3)
        assert batches == [1], "失败之后不该再有第二批"

    def test_pagination(self, sink: SQLiteSink):
        for i in range(10):
            sink.submit(_record(ts=time.time() + i, url=f"http://example.com/{i}"))
        _drain(sink, 10)
        page1 = sink.query(limit=4, offset=0)
        page2 = sink.query(limit=4, offset=4)
        assert {r.url for r in page1} & {r.url for r in page2} == set()


class TestRecorderWithSink:
    def test_enabled_sqlite_is_used_for_queries(self, tmp_path: Path):
        recorder = TraceRecorder(TraceSettings(sqlite_enabled=True, sqlite_path=str(tmp_path / "t.db"), memory_size=2))
        try:
            for i in range(6):
                recorder.emit(_trace(recorder, url=f"http://example.com/{i}"), 1)
            assert recorder.sink is not None
            _drain(recorder.sink, 6)
            rows, source = recorder.query(limit=100)
            assert source == "sqlite"
            # 内存只留了 2 条，但落盘的 6 条都能查到
            assert len(rows) == 6
            assert len(recorder.recent(limit=100)) == 2
        finally:
            recorder.close()

    def test_falls_back_to_memory_when_disabled(self):
        recorder = TraceRecorder(TraceSettings(memory_size=5))
        recorder.emit(_trace(recorder), 1)
        _, source = recorder.query()
        assert source == "memory"

    def test_unwritable_path_degrades_instead_of_crashing(self, tmp_path: Path):
        """建不了库就退回纯内存，服务照常起——链路日志不该是启动的硬依赖。"""
        blocked = tmp_path / "afile"
        blocked.write_text("x", encoding="utf-8")
        recorder = TraceRecorder(TraceSettings(sqlite_enabled=True, sqlite_path=str(blocked / "sub" / "t.db")))
        try:
            recorder.emit(_trace(recorder), 1)
            assert recorder.sink is None
            assert recorder.status()["source"] == "memory"
            assert len(recorder.recent()) == 1
        finally:
            recorder.close()

    def test_status_exposes_drop_count(self, tmp_path: Path):
        recorder = TraceRecorder(TraceSettings(sqlite_enabled=True, sqlite_path=str(tmp_path / "t.db")))
        try:
            info = recorder.status()
            assert info["sqlite_enabled"] is True
            assert info["dropped"] == 0
            assert info["retention_days"] == 30
        finally:
            recorder.close()

    def test_stats_includes_window_and_daily(self, tmp_path: Path):
        recorder = TraceRecorder(TraceSettings(sqlite_enabled=True, sqlite_path=str(tmp_path / "t.db")))
        try:
            recorder.emit(_trace(recorder), 3)
            assert recorder.sink is not None
            _drain(recorder.sink, 1)
            stats = recorder.stats(days=30)
            assert stats["window"]["total"] == 1
            assert stats["window_days"] == 30
            assert stats["daily"]
            assert stats["process"]["total"] == 1
        finally:
            recorder.close()


class TestSettings:
    def test_defaults_are_memory_only(self):
        settings = TraceSettings.from_config({})
        assert settings.sqlite_enabled is False
        assert settings.memory_size == 500
        assert settings.retention_days == 30

    def test_from_config(self):
        settings = TraceSettings.from_config(
            {"memory_size": 10, "sqlite_enabled": True, "retention_days": 7, "only_errors": True}
        )
        assert (settings.memory_size, settings.sqlite_enabled, settings.retention_days) == (10, True, 7)
        assert settings.only_errors is True

    def test_garbage_values_fall_back(self):
        settings = TraceSettings.from_config({"memory_size": "abc", "retention_days": None})
        assert settings.memory_size == 500
        assert settings.retention_days == 30

    def test_node_id_from_argument_wins(self):
        assert TraceSettings.from_config({"node_id": "in-config"}, node_id="explicit").node_id == "explicit"


class TestHostColumn:
    """host 单独存一列，且与限流器用的 host_of 是同一套定义。

    以前是查询时从 url 现算：Python 侧用 host_of（去端口），而 SQL 里自己拼的
    instr/substr 会保留端口、也处理不了 IPv6 字面量——于是"排行榜里的 host"和
    "限流用的 host"是两个东西。
    """

    @pytest.mark.parametrize(
        "url",
        [
            "http://a.com/x",
            "https://b.com",
            "http://c.com:8080/p?q=1",
            "http://127.0.0.1:18080/html",
            "http://[::1]:9527/v",
        ],
    )
    def test_host_matches_the_rate_limiter(self, sink: SQLiteSink, url: str):
        from ipclick.limiter import host_of

        sink.submit(_record(url=url))
        _drain(sink, 1)
        assert sink.top_hosts(limit=5)[0]["host"] == host_of(url)

    def test_unparseable_value_is_shown_as_is(self, sink: SQLiteSink):
        """解析不出协议时把存的值原样当 host。

        走到这里只有一种情况：``record_url = false``，此时 url 列里存的**已经是**
        host。真正畸形的 URL 到不了这里——``validate_url`` 只放行 http/https，
        请求在进适配器之前就被拒了。
        """
        sink.submit(_record(url="example.com"))
        _drain(sink, 1)
        assert sink.top_hosts(limit=5)[0]["host"] == "example.com"

    def test_ranking_has_no_silent_cap(self, sink: SQLiteSink):
        """原实现是 query(limit=20000) 再在 Python 里聚合——超过两万条之后排行
        只反映其中一部分，而页面上看不出来。现在是 SQL 侧 GROUP BY，没有这个上限。
        """
        for i in range(2000):
            sink.submit(_record(url=f"http://site{i % 3}.com/p/{i}"))
        _drain(sink, 2000)
        ranking = sink.top_hosts(limit=10)
        assert sum(h["total"] for h in ranking) == 2000

    def test_old_database_gets_the_column(self, tmp_path: Path):
        """老库要能平滑升级：加列 + 回填，一行数据都不能丢。"""
        import sqlite3 as sq

        path = tmp_path / "old.db"
        conn = sq.connect(path)
        conn.executescript(
            "CREATE TABLE traces (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,"
            " uuid TEXT, node_id TEXT, adapter TEXT, method TEXT, url TEXT,"
            " status_code INTEGER NOT NULL, duration_ms INTEGER, size INTEGER, attempts INTEGER,"
            " forwarded INTEGER, queued_ms INTEGER, error TEXT, stream INTEGER);"
        )
        _ = conn.execute(
            "INSERT INTO traces (ts, url, status_code, duration_ms) VALUES (?,?,?,?)",
            (time.time(), "http://legacy.example.com/a", 200, 5),
        )
        conn.commit()
        conn.close()

        sink = SQLiteSink(str(path), retention_days=0)
        try:
            assert sink.count() == 1, "老数据不能丢"
            assert sink.top_hosts(limit=5)[0]["host"] == "legacy.example.com"
        finally:
            sink.close()

    def test_backfill_keeps_host_only_rows(self, tmp_path: Path):
        """老库里 record_url = false 写的行，url 列存的**已经是** host。

        回填的 SQL 认的是 "://"，这些行没有——早先的写法会把它们统一填成 "-"，
        于是关掉 record_url 的部署升级之后整个排行是一片 "-"。要和
        TraceRecord.host 的兜底保持一致：原样用。
        """
        import sqlite3 as sq

        path = tmp_path / "host-only.db"
        conn = sq.connect(path)
        conn.executescript(
            "CREATE TABLE traces (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,"
            " uuid TEXT, node_id TEXT, adapter TEXT, method TEXT, url TEXT,"
            " status_code INTEGER NOT NULL, duration_ms INTEGER, size INTEGER, attempts INTEGER,"
            " forwarded INTEGER, queued_ms INTEGER, error TEXT, stream INTEGER);"
        )
        for url in ("legacy.example.com", ""):
            _ = conn.execute(
                "INSERT INTO traces (ts, url, status_code, duration_ms) VALUES (?,?,?,?)",
                (time.time(), url, 200, 5),
            )
        conn.commit()
        conn.close()

        sink = SQLiteSink(str(path), retention_days=0)
        try:
            hosts = {h["host"] for h in sink.top_hosts(limit=5)}
            assert "legacy.example.com" in hosts, "只存了 host 的老行要原样保留"
            assert "-" in hosts, "url 为空的行才该是 -"
        finally:
            sink.close()


class TestWindowStatsCache:
    """跨天聚合带短 TTL 缓存：请求流页每 3 秒刷一次，而这些数字描述的是 30 天窗口。"""

    def test_repeated_stats_hits_the_cache(self, tmp_path: Path):
        recorder = TraceRecorder(TraceSettings(sqlite_enabled=True, sqlite_path=str(tmp_path / "t.db")))
        try:
            recorder.emit(_trace(recorder), 1)
            assert recorder.sink is not None
            _drain(recorder.sink, 1)

            calls: list[int] = []
            original = recorder.sink.daily
            recorder.sink.daily = lambda days=30: (calls.append(1), original(days))[1]  # pyright: ignore[reportAttributeAccessIssue]

            for _ in range(5):
                _ = recorder.stats()
            assert len(calls) == 1, f"5 次 stats() 只该算一次跨天聚合，实际 {len(calls)} 次"
        finally:
            recorder.close()

    def test_live_numbers_are_not_cached(self, tmp_path: Path):
        """在途数、总数这些实时值不能走缓存，否则看板会"卡住"不动。"""
        recorder = TraceRecorder(TraceSettings(sqlite_enabled=True, sqlite_path=str(tmp_path / "t.db")))
        try:
            before = recorder.stats()["process"]["total"]
            recorder.emit(_trace(recorder), 1)
            assert recorder.stats()["process"]["total"] == before + 1
        finally:
            recorder.close()


class TestKeywordFilterIsLiteral:
    """URL 关键字筛选是**子串**匹配，不是通配符匹配。

    LIKE 里 % 和 _ 是通配符，而 URL 里这两个字符都很常见（/api_v2/、utm_source=、
    100%off）。不转义的话用户以为筛出来的是精确子串，实际不是。
    """

    def test_underscore_is_literal(self, sink: SQLiteSink):
        sink.submit(_record(url="http://a.com/api_v2/x"))
        sink.submit(_record(url="http://a.com/apiXv2/y"))
        _drain(sink, 2)
        hits = [r.url for r in sink.query(keyword="api_v2")]
        assert hits == ["http://a.com/api_v2/x"], hits

    def test_percent_is_literal(self, sink: SQLiteSink):
        sink.submit(_record(url="http://a.com/100%off"))
        sink.submit(_record(url="http://a.com/other"))
        _drain(sink, 2)
        hits = [r.url for r in sink.query(keyword="100%off")]
        assert hits == ["http://a.com/100%off"], hits

    def test_plain_substring_still_matches(self, sink: SQLiteSink):
        """别把转义做过头，普通子串要照常命中。"""
        for i in range(3):
            sink.submit(_record(url=f"http://a.com/p/{i}"))
        _drain(sink, 3)
        assert len(sink.query(keyword="a.com")) == 3

    def test_memory_and_sqlite_agree(self, tmp_path: Path):
        """内存侧和 SQL 侧的筛选行为必须一致，否则开不开落盘筛出来的东西不一样。"""
        recorder = TraceRecorder(TraceSettings(sqlite_enabled=True, sqlite_path=str(tmp_path / "t.db"), memory_size=50))
        try:
            for url in ("http://a.com/api_v2/x", "http://a.com/apiXv2/y"):
                recorder.emit(_trace(recorder, url=url), 1)
            assert recorder.sink is not None
            _drain(recorder.sink, 2)

            from_sqlite = {r.url for r in recorder.sink.query(keyword="api_v2")}
            from_memory = {r.url for r in recorder.recent(keyword="api_v2")}
            assert from_sqlite == from_memory == {"http://a.com/api_v2/x"}
        finally:
            recorder.close()


class TestHostWithoutFullUrl:
    def test_record_url_off_still_yields_a_host(self):
        """回归：record_url=false 时 url 里存的已经是 host，host_of 对它解析失败，
        整个目标站点排行会全变成 "-"。
        """
        recorder = TraceRecorder(TraceSettings(memory_size=5, record_url=False))
        recorder.emit(_trace(recorder, url="http://example.com/secret?token=x"), 1)
        record = recorder.recent()[0]
        assert record.url == "example.com"
        assert record.host == "example.com"

    def test_ranking_groups_by_real_host_when_url_is_off(self, tmp_path: Path):
        recorder = TraceRecorder(
            TraceSettings(sqlite_enabled=True, sqlite_path=str(tmp_path / "t.db"), record_url=False)
        )
        try:
            for url in ("http://a.com/x", "http://a.com/y", "http://b.com/z"):
                recorder.emit(_trace(recorder, url=url), 1)
            assert recorder.sink is not None
            _drain(recorder.sink, 3)
            ranking = {h["host"]: h["total"] for h in recorder.sink.top_hosts()}
            assert ranking == {"a.com": 2, "b.com": 1}
        finally:
            recorder.close()
