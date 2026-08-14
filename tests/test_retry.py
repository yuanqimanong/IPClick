"""adapters/base.py 的重试装饰器。

全部用假适配器，不产生真实网络请求，也不真的 sleep。
"""

from typing import Any

import pytest

from ipclick.adapters.base import MAX_RETRY_DELAY, DownloaderAdapter, _backoff, _coerce_delay, retry
from ipclick.dto.response import Response


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """拦截 time.sleep，既让测试瞬间跑完，也能断言退避时长。"""
    slept: list[float] = []
    monkeypatch.setattr("ipclick.adapters.base.time.sleep", lambda s: slept.append(s))
    return slept


class FlakyAdapter(DownloaderAdapter):
    """前 ``fail_times`` 次抛异常，之后返回 200。"""

    adapter_name = "flaky"

    def __init__(self, fail_times: int = 0, status_code: int = 200):
        super().__init__()
        self.fail_times = fail_times
        self.status_code = status_code
        self.calls = 0

    @retry()
    def download(self, url: str, **kwargs: Any) -> Response:  # type: ignore[override]
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError(f"attempt {self.calls} failed")
        return Response(url=url, status_code=self.status_code, content=b"ok")


class TestRetryCounts:
    def test_succeeds_without_retry(self):
        adapter = FlakyAdapter(fail_times=0)
        resp = adapter.download("http://a.com")
        assert resp.status_code == 200
        assert adapter.calls == 1

    def test_retries_then_succeeds(self):
        adapter = FlakyAdapter(fail_times=2)
        resp = adapter.download("http://a.com", max_retries=3)
        assert resp.status_code == 200
        assert adapter.calls == 3

    def test_exhausted_retries_return_error_response(self):
        adapter = FlakyAdapter(fail_times=99)
        resp = adapter.download("http://a.com", max_retries=2)
        assert resp.status_code == -1
        assert isinstance(resp.exception, ConnectionError)
        # 1 次初始 + 2 次重试
        assert adapter.calls == 3

    def test_max_retries_zero_means_exactly_one_attempt(self, _no_real_sleep: list[float]):
        """回归：``kwargs.get("max_retries") or default`` 会把 0 当成未传，
        于是 max_retries=0 实际重试了 3 次。"""
        adapter = FlakyAdapter(fail_times=99)
        resp = adapter.download("http://a.com", max_retries=0)
        assert adapter.calls == 1
        assert resp.status_code == -1
        assert _no_real_sleep == []

    def test_retry_delay_zero_is_honoured(self, _no_real_sleep: list[float]):
        """回归：retry_delay=0 以前同样被 falsy 判断吃掉。"""
        adapter = FlakyAdapter(fail_times=1)
        adapter.download("http://a.com", max_retries=2, retry_delay=0)
        assert all(s == 0 for s in _no_real_sleep)


class TestStatusCodeRetry:
    def test_5xx_triggers_retry(self):
        adapter = FlakyAdapter(fail_times=0, status_code=503)
        adapter.download("http://a.com", max_retries=2)
        assert adapter.calls == 3

    def test_allowed_status_code_suppresses_retry(self):
        adapter = FlakyAdapter(fail_times=0, status_code=503)
        adapter.download("http://a.com", max_retries=2, allowed_status_codes=[200, 503])
        assert adapter.calls == 1

    def test_404_is_not_retried(self):
        adapter = FlakyAdapter(fail_times=0, status_code=404)
        adapter.download("http://a.com", max_retries=3)
        assert adapter.calls == 1


class TestBackoff:
    def test_backoff_is_capped(self):
        """回归：原来是 min(2**attempt, 600)，大 attempt 会让 worker 睡 10 分钟。"""
        for attempt in range(20):
            assert _backoff(attempt, base_delay=2.0) <= MAX_RETRY_DELAY * 1.2

    def test_backoff_grows_with_attempt(self):
        assert _backoff(0, 1.0) < _backoff(5, 1.0)

    def test_sleep_durations_never_exceed_cap(self, _no_real_sleep: list[float]):
        adapter = FlakyAdapter(fail_times=99)
        adapter.download("http://a.com", max_retries=8, retry_delay=5)
        assert _no_real_sleep
        assert max(_no_real_sleep) <= MAX_RETRY_DELAY * 1.2

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(None, 1.0), (2.5, 2.5), ("3", 3.0), ("abc", 1.0), ([], 1.0)],
    )
    def test_coerce_delay(self, value: Any, expected: float):
        assert _coerce_delay(value, default=1.0) == expected

    def test_coerce_delay_tuple_stays_in_range(self):
        for _ in range(50):
            assert 1.0 <= _coerce_delay((1, 3), default=0.0) <= 3.0


class TestRetryLogging:
    def test_retry_is_logged(self):
        """回归：日志以前被 ``hasattr(self, "logger")`` 挡住，而适配器没有该属性，
        导致整个重试过程完全静默。"""
        from ipclick.utils.log_util import logger

        messages: list[str] = []
        handler_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            FlakyAdapter(fail_times=1).download("http://a.com", max_retries=2)
        finally:
            logger.remove(handler_id)

        assert any("retrying" in m for m in messages), messages

    def test_status_code_retry_is_logged(self):
        from ipclick.utils.log_util import logger

        messages: list[str] = []
        handler_id = logger.add(messages.append, level="WARNING", format="{message}")
        try:
            FlakyAdapter(fail_times=0, status_code=503).download("http://a.com", max_retries=1)
        finally:
            logger.remove(handler_id)

        assert any("503" in m for m in messages), messages


class TestParseExtraKwargs:
    @pytest.mark.parametrize("raw", [None, "", "   ", "not json", "[1,2]", "null"])
    def test_malformed_kwargs_yield_empty_dict(self, raw: str | None):
        """回归：curl_cffi 适配器过去无条件 json.loads(kwargs)，空串直接抛异常。"""
        assert DownloaderAdapter.parse_extra_kwargs(raw) == {}

    def test_valid_kwargs_parsed(self):
        assert DownloaderAdapter.parse_extra_kwargs('{"a": 1}') == {"a": 1}


class TestAdapterErrorIsNotRetried:
    """AdapterError = "本服务端做不到"，重试改变不了任何一条。

    代价特别大：浏览器请求一次的预算就是几十上百秒，被重试 3 次变成四倍。
    实测一次「试一试」点击因此挂了 296 秒。
    """

    def test_adapter_error_propagates_immediately(self):
        from ipclick.adapters.base import retry
        from ipclick.exceptions import AdapterError

        calls: list[int] = []

        class Boom:
            adapter_name = "fake"
            max_retries = 3
            retry_delay = 0.01

            @retry()
            def download(self, url: str, **kwargs: object) -> Response:
                calls.append(1)
                raise AdapterError("浏览器任务超过 150 秒未返回")

        with pytest.raises(AdapterError):
            _ = Boom().download("http://example.com")
        assert calls == [1], f"只该尝试一次，实际 {len(calls)} 次"

    def test_transient_errors_are_still_retried(self):
        """别把重试整个关掉了——真的网络抖动仍然要重试。"""
        from ipclick.adapters.base import retry

        calls: list[int] = []

        class Flaky:
            adapter_name = "fake"
            max_retries = 2
            retry_delay = 0.01

            @retry()
            def download(self, url: str, **kwargs: object) -> Response:
                calls.append(1)
                raise ConnectionError("connection reset")

        resp = Flaky().download("http://example.com")
        assert resp.status_code == -1
        assert len(calls) == 3, f"应该尝试 1+2 次，实际 {len(calls)} 次"
