from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
import sqlite3
import threading
import time

import pytest

from ipclick.trace import SQLiteSink, TraceReader, TraceRecord, classify_status
from ipclick.trace.records import matches
from ipclick.trace.store import _status_clause


_GENEROUS = 30.0


def _wait_until(predicate: Callable[[], bool], timeout: float = _GENEROUS) -> bool:
    """轮询等待条件成立。

    这里刻意不用 time.sleep(常数) 来给并发定序：那是在赌"这一步能在 X 毫秒内跑完"，
    在负载高的 CI runner 上会随机翻车（本测试就这么红过一次）。等状态而不是等时间。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _record() -> TraceRecord:
    return TraceRecord(
        ts=time.time(),
        uuid="request-1",
        node_id="node-1",
        adapter="curl_cffi",
        method="GET",
        url="https://example.com",
        status_code=200,
        duration_ms=1,
        size=2,
    )


def test_error_status_alias_matches_all_failed_records() -> None:
    success = _record()
    http_error = replace(success, status_code=500)
    transport_error = replace(success, status_code=-1)

    assert _status_clause("error") == _status_clause("failed")
    assert not matches(success, "error", "", "")
    assert matches(http_error, "error", "", "")
    assert matches(transport_error, "error", "", "")


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [(-1, "failure"), (0, "failure"), (100, "failure"), (200, "2xx"), (299, "2xx"), (300, "3xx")],
)
def test_status_classification_uses_real_http_ranges(status_code: int, expected: str) -> None:
    assert classify_status(status_code) == expected


def test_informational_status_is_failed_in_memory_and_sql_filters() -> None:
    informational = replace(_record(), status_code=100)

    assert not informational.ok
    assert informational.status_class == "failure"
    assert matches(informational, "failed", "", "")
    assert _status_clause("failure") == "status_code < 200"


def test_close_delivers_sentinel_when_queue_starts_full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original_run = SQLiteSink._run
    worker_started = threading.Event()
    allow_worker = threading.Event()

    def delayed_run(self: SQLiteSink) -> None:
        worker_started.set()
        # 超时只是兜底，正常路径永远是被测试 set 唤醒的——所以给得宽松些。
        allow_worker.wait(timeout=_GENEROUS)
        original_run(self)

    monkeypatch.setattr(SQLiteSink, "_run", delayed_run)
    sink = SQLiteSink(str(tmp_path / "trace.db"), queue_size=1)
    assert worker_started.wait(timeout=_GENEROUS)
    sink.submit(_record())

    closer = threading.Thread(target=sink.close, kwargs={"timeout": _GENEROUS})
    closer.start()
    # 等 close 把 stop 置上：它就在 put_nowait 之前，此刻队列必然还是满的，
    # 哨兵一定入不了队——正是本用例要覆盖的那条路径。
    assert _wait_until(sink._stop.is_set)
    allow_worker.set()
    closer.join(timeout=_GENEROUS)

    assert not closer.is_alive()
    sink._thread.join(timeout=_GENEROUS)
    assert not sink._thread.is_alive()
    assert sink.written == 1


def test_close_eventually_stops_after_timeout_with_a_full_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_run = SQLiteSink._run
    worker_started = threading.Event()
    allow_worker = threading.Event()

    def delayed_run(self: SQLiteSink) -> None:
        worker_started.set()
        allow_worker.wait(timeout=_GENEROUS)
        original_run(self)

    monkeypatch.setattr(SQLiteSink, "_run", delayed_run)
    sink = SQLiteSink(str(tmp_path / "late-trace.db"), queue_size=1)
    assert worker_started.wait(timeout=_GENEROUS)
    sink.submit(_record())

    # 这里的 0.01 是被测行为本身（关闭超时），不是给并发定序用的，保持原样。
    sink.close(timeout=0.01)
    assert sink._thread.is_alive()
    allow_worker.set()
    sink._thread.join(timeout=_GENEROUS)

    assert not sink._thread.is_alive()
    assert sink.written == 1


def test_readonly_trace_uri_escapes_fragment_characters(tmp_path: Path) -> None:
    path = tmp_path / "trace#fragment.db"
    sink = SQLiteSink(str(path))
    sink.submit(_record())
    sink.close(timeout=2.0)

    reader = TraceReader(str(path))
    records = reader.query(limit=10)

    assert len(records) == 1
    assert records[0].uuid == "request-1"


def test_retention_purge_actually_reclaims_disk_space(tmp_path: Path) -> None:
    """保留期清理必须真的把文件缩回去。

    ``PRAGMA auto_vacuum=INCREMENTAL`` 原来排在 ``journal_mode=WAL`` **之后**，而 WAL
    那句已经把库文件初始化了——auto_vacuum 于是被静默忽略（读回来是 0），``_purge()``
    里那句 ``incremental_vacuum`` 永远是空操作。结果是：保留期到了、记录删掉了，
    文件却永远停在历史最高水位，``status()["db_bytes"]`` 也永远不降。
    """
    path = tmp_path / "trace.db"
    sink = SQLiteSink(str(path), retention_days=1)
    try:
        with sqlite3.connect(str(path)) as probe:
            assert probe.execute("PRAGMA auto_vacuum").fetchone()[0] == 2, "auto_vacuum 没生效"

        stale = time.time() - 10 * 86400
        with sqlite3.connect(str(path)) as writer:
            _ = writer.executemany(
                "INSERT INTO traces (ts, uuid, adapter, method, url, host, status_code, size, duration_ms)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        stale,
                        f"u{i}",
                        "curl_cffi",
                        "GET",
                        f"https://example.com/{'x' * 200}/{i}",
                        "example.com",
                        200,
                        0,
                        1,
                    )
                    for i in range(4000)
                ],
            )
            writer.commit()

        # WAL 模式下新数据先落在 -wal 里，量文件大小前必须先 checkpoint
        def _size() -> int:
            with sqlite3.connect(str(path)) as conn:
                _ = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return path.stat().st_size

        grown = _size()
        sink._purge()
        shrunk = _size()

        with sqlite3.connect(str(path)) as probe:
            assert probe.execute("SELECT count(*) FROM traces").fetchone()[0] == 0
        assert shrunk < grown, f"清理后文件没缩小：{grown} -> {shrunk}"
    finally:
        sink.close()


def test_records_dropped_after_falling_back_to_memory_are_counted(tmp_path: Path) -> None:
    """退化到内存模式之后被扔掉的记录也要计入 dropped。

    原来 failed 之后 submit 直接 return，dropped 一直是 0——面板上"丢弃 0 条"和
    "100% 没落盘"同时成立，运维看不出任何异常。
    """
    sink = SQLiteSink(str(tmp_path / "trace.db"))
    try:
        sink.failed = True
        before = sink.dropped
        for i in range(5):
            sink.submit(
                TraceRecord(
                    ts=time.time(),
                    uuid=f"u{i}",
                    node_id="n1",
                    adapter="curl_cffi",
                    method="GET",
                    url="https://e/",
                    status_code=200,
                    duration_ms=1,
                    size=0,
                )
            )
        assert sink.dropped == before + 5
    finally:
        sink.close()


def test_record_url_off_also_scrubs_urls_out_of_the_error_text() -> None:
    """record_url = false 承诺"只记 host"，error 字段原来是原样抄进去的。

    适配器的错误信息里经常嵌着完整 URL（重定向超上限、浏览器导航失败都会带上），
    于是 ?api_key=… 照样落进 SQLite、照样显示在请求流页面上——而运维以为自己已经
    把完整 URL 关掉了。
    """
    from ipclick.trace import TraceRecorder, TraceSettings

    recorder = TraceRecorder(TraceSettings(record_url=False, memory_size=10))
    try:
        with recorder.track_request(
            adapter="curl_cffi", method="GET", url="https://api.example.com/v1?api_key=SECRET"
        ) as tr:
            tr.status_code = -1
            tr.error = "重定向次数超过上限 10：最后停在 https://api.example.com/v1?api_key=SECRET"

        record = recorder.recent(1)[0]
    finally:
        recorder.close()

    assert record.url == "api.example.com"
    assert "SECRET" not in record.error
    assert "api.example.com" in record.error


def test_record_url_on_keeps_the_error_text_intact() -> None:
    """默认打开时不能反过来把错误信息也削掉。"""
    from ipclick.trace import TraceRecorder, TraceSettings

    recorder = TraceRecorder(TraceSettings(record_url=True, memory_size=10))
    try:
        with recorder.track_request(
            adapter="curl_cffi", method="GET", url="https://api.example.com/v1?api_key=SECRET"
        ) as tr:
            tr.status_code = -1
            tr.error = "最后停在 https://api.example.com/v1?api_key=SECRET"

        record = recorder.recent(1)[0]
    finally:
        recorder.close()

    assert record.url == "https://api.example.com/v1?api_key=SECRET"
    assert "api_key=SECRET" in record.error


def test_write_connections_get_synchronous_normal(tmp_path: Path) -> None:
    """synchronous 是按连接生效的，不写进文件头。

    原来只在 _init_db 那条连接上设过，而它随即关闭：此后每次 _write / _purge 新开的
    连接都退回默认 FULL，每 0.5 秒一次的批量提交都要整盘 fsync——正是这条 pragma
    想省掉的开销。（journal_mode 反过来是持久化的，所以那一项本来就没问题。）
    """
    sink = SQLiteSink(str(tmp_path / "t.db"), retention_days=1, queue_size=10)
    try:
        conn = sink._connect()
        try:
            assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # 1 = NORMAL
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        finally:
            conn.close()
    finally:
        sink.close()
