from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
import threading
import time

import pytest

from ipclick.trace import SQLiteSink, TraceReader, TraceRecord, _matches, _status_clause, classify_status


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
    assert not _matches(success, "error", "", "")
    assert _matches(http_error, "error", "", "")
    assert _matches(transport_error, "error", "", "")


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
    assert _matches(informational, "failed", "", "")
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
