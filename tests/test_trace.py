from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import threading
import time

import pytest

from ipclick.trace import SQLiteSink, TraceReader, TraceRecord, _matches, _status_clause, classify_status


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
        allow_worker.wait(timeout=1.0)
        original_run(self)

    monkeypatch.setattr(SQLiteSink, "_run", delayed_run)
    sink = SQLiteSink(str(tmp_path / "trace.db"), queue_size=1)
    assert worker_started.wait(timeout=1.0)
    sink.submit(_record())

    closer = threading.Thread(target=sink.close, kwargs={"timeout": 1.0})
    closer.start()
    time.sleep(0.05)
    allow_worker.set()
    closer.join(timeout=1.0)

    assert not closer.is_alive()
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
        allow_worker.wait(timeout=1.0)
        original_run(self)

    monkeypatch.setattr(SQLiteSink, "_run", delayed_run)
    sink = SQLiteSink(str(tmp_path / "late-trace.db"), queue_size=1)
    assert worker_started.wait(timeout=1.0)
    sink.submit(_record())

    sink.close(timeout=0.01)
    assert sink._thread.is_alive()
    allow_worker.set()
    sink._thread.join(timeout=1.0)

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
