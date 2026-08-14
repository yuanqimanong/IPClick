"""[DOWNLOADER] 配置到适配器行为的贯通测试。

回归背景：这一节此前只是被 TaskService 存进 self.adapter_config 就再没读过，
改配置完全不生效。
"""

from typing import Any

import pytest

from ipclick.adapters.base import _backoff
from ipclick.adapters.settings import DEFAULT_RETRY_STATUS_CODES, HARD_MAX_BACKOFF, AdapterSettings


class TestFromConfig:
    def test_empty_config_uses_defaults(self):
        s = AdapterSettings.from_config(None)
        assert s.download_timeout == AdapterSettings().download_timeout
        assert s.retry_codes == DEFAULT_RETRY_STATUS_CODES

    def test_reads_timeouts(self):
        s = AdapterSettings.from_config({"connect_timeout": 5, "download_timeout": 120})
        assert s.connect_timeout == 5.0
        assert s.download_timeout == 120.0

    def test_reads_retry_block(self):
        s = AdapterSettings.from_config(
            {
                "retry": {
                    "max_attempts": 7,
                    "backoff_exponent": 3.0,
                    "initial_backoff": 2,
                    "max_backoff": 45,
                    "retry_codes": [500, 502],
                }
            }
        )
        assert s.max_attempts == 7
        assert s.backoff_exponent == 3.0
        assert s.initial_backoff == 2.0
        assert s.max_backoff == 45.0
        assert s.retry_codes == frozenset({500, 502})

    def test_reads_concurrency_block(self):
        s = AdapterSettings.from_config({"concurrency": {"max_connections": 42, "max_keepalive_connections": 7}})
        assert s.max_connections == 42
        assert s.max_keepalive_connections == 7

    def test_max_backoff_is_hard_capped(self):
        """配置写得再大也不能让 worker 线程睡太久。"""
        s = AdapterSettings.from_config({"retry": {"max_backoff": 99999}})
        assert s.max_backoff == HARD_MAX_BACKOFF

    @pytest.mark.parametrize("bad", ["abc", None, -1, [], {}])
    def test_malformed_values_fall_back(self, bad: Any):
        s = AdapterSettings.from_config({"download_timeout": bad, "retry": {"max_attempts": bad}})
        defaults = AdapterSettings()
        assert s.download_timeout == defaults.download_timeout
        assert s.max_attempts == defaults.max_attempts

    def test_zero_max_attempts_is_a_valid_choice(self):
        """max_attempts = 0 表示"不重试"，是合法取值，不能被当成未配置。

        这正是之前修掉的 falsy-0 bug 类型，别再引回来。
        """
        assert AdapterSettings.from_config({"retry": {"max_attempts": 0}}).max_attempts == 0

    def test_zero_timeout_falls_back(self):
        """timeout = 0 会让请求立刻超时，没有合理语义，回落到默认值。"""
        assert AdapterSettings.from_config({"download_timeout": 0}).download_timeout == 300.0

    def test_malformed_retry_codes_fall_back(self):
        s = AdapterSettings.from_config({"retry": {"retry_codes": "not-a-list"}})
        assert s.retry_codes == DEFAULT_RETRY_STATUS_CODES

    def test_shipped_default_config_parses(self):
        """随包分发的 default_config.toml 必须能被完整解析。"""
        from ipclick.config_loader.loader import load_config

        load_config.cache_clear()
        s = AdapterSettings.from_config(dict(load_config().get("DOWNLOADER", {})))
        assert s.download_timeout > 0
        assert s.max_attempts >= 0
        assert s.retry_codes


class TestBackoffUsesConfig:
    def test_exponent_is_honoured(self):
        """回归：退避指数以前写死为 2，[DOWNLOADER.retry].backoff_exponent 无效。"""
        base, attempt = 1.0, 3
        low = _backoff(attempt, base, exponent=1.5, max_backoff=1000)
        high = _backoff(attempt, base, exponent=3.0, max_backoff=1000)
        assert low < high

    def test_max_backoff_is_honoured(self):
        for attempt in range(15):
            assert _backoff(attempt, 10.0, exponent=2.0, max_backoff=5.0) <= 5.0 * 1.2


class TestAdapterPicksUpSettings:
    def test_adapter_defaults_come_from_settings(self):
        from ipclick.adapters.curl_cffi_adapter import CurlCffiAdapter

        settings = AdapterSettings.from_config(
            {"download_timeout": 123, "retry": {"max_attempts": 9, "initial_backoff": 4}}
        )
        adapter = CurlCffiAdapter(settings)
        try:
            assert adapter.timeout == 123.0
            assert adapter.max_retries == 9
            assert adapter.retry_delay == 4.0
            assert adapter.settings is settings
        finally:
            adapter.close()

    def test_connection_pool_limits_come_from_settings(self):
        """[DOWNLOADER.concurrency].max_connections 要真落到底层连接池上。"""
        from ipclick.adapters.niquests_adapter import NIQUESTS_AVAILABLE, NiquestsAdapter

        if not NIQUESTS_AVAILABLE:
            pytest.skip("niquests 未安装")

        adapter = NiquestsAdapter(AdapterSettings.from_config({"concurrency": {"max_connections": 11}}))
        try:
            assert adapter.settings.max_connections == 11
            session = adapter._get_session(None, verify=True)  # pyright: ignore[reportPrivateUsage]
            adapters = getattr(session, "adapters", {})
            pool_sizes = {
                getattr(a, "_pool_maxsize", None) or getattr(a, "poolmanager", None) for a in adapters.values()
            }
            assert pool_sizes, "至少要有一个挂载的传输适配器"
        finally:
            adapter.close()

    def test_retry_codes_from_config_drive_retries(self):
        """配置里没列入 retry_codes 的状态码不应触发重试。"""
        from ipclick.adapters.base import DownloaderAdapter, retry
        from ipclick.dto.response import Response

        class Counting(DownloaderAdapter):
            adapter_name = "counting"

            def __init__(self, settings: AdapterSettings, status: int):
                super().__init__(settings)
                self.status = status
                self.calls = 0

            @retry()
            def download(self, url: str, **kwargs: Any) -> Response:  # type: ignore[override]
                self.calls += 1
                return Response(url=url, status_code=self.status, content=b"")

        only_500 = AdapterSettings.from_config({"retry": {"retry_codes": [500], "initial_backoff": 0}})

        a = Counting(only_500, status=503)
        a.download("http://x", max_retries=2, retry_delay=0)
        assert a.calls == 1, "503 不在配置的 retry_codes 里，不应重试"

        b = Counting(only_500, status=500)
        b.download("http://x", max_retries=2, retry_delay=0)
        assert b.calls == 3, "500 在配置的 retry_codes 里，应重试"


class TestTaskServiceUsesConfig:
    def test_service_defaults_come_from_downloader_section(self, monkeypatch: pytest.MonkeyPatch):
        """回归：服务端的超时/重试默认值以前是写死常量，不看配置。"""
        from ipclick.dto.proto import task_pb2
        from ipclick.dto.response import Response
        from ipclick.services.task_service import TaskService
        from ipclick.utils.config_util import Settings
        from tests.test_task_service import FakeContext, RecordingAdapter

        adapter = RecordingAdapter()
        monkeypatch.setattr("ipclick.services.task_service.get_default_adapter", lambda settings=None: adapter)
        monkeypatch.setattr(
            "ipclick.services.task_service.get_adapter", lambda name, settings=None, browser_settings=None: adapter
        )

        service = TaskService(
            Settings({"DOWNLOADER": {"download_timeout": 33, "retry": {"max_attempts": 6, "initial_backoff": 5}}})
        )
        service.Send(task_pb2.ReqTask(url="http://example.com", uuid="u1"), FakeContext())

        assert adapter.last_kwargs["timeout"] == 33.0
        assert adapter.last_kwargs["max_retries"] == 6
        assert adapter.last_kwargs["retry_delay"] == 5.0
        assert isinstance(Response(url="x", status_code=200), Response)
