"""0.6.0 引入的性能相关开关与热路径修复。

这些项都是压测里量出来的，测试保的是"以后别改回去"：

* ``[SERVER].processes`` —— 多进程分片（GIL 是单进程的吞吐天花板）
* ``[SERVER].max_concurrent_rpcs`` —— 准入上限不再由线程数推导
* ``[SERVER].max_concurrent_streams`` —— 单客户端并发不再被写死的 100 卡住
* ``[SERVER].compression`` —— 响应压缩可选
* User-Agent 池化 —— ``fake_useragent`` 每次取值 2.82ms，曾在每请求热路径上
* UNAVAILABLE 提示分流 —— 拒流和连不上排查方向相反
"""

from typing import Any

import grpc
import pytest

from ipclick.exceptions import ConfigError
from ipclick.server import IPClickServer, _resolve_processes
from ipclick.utils.config_util import Settings


def _config(tmp_path: Any, body: str) -> str:
    path = tmp_path / "ipclick.toml"
    path.write_text(body, encoding="utf-8")
    return str(path)


class TestProcesses:
    """``[SERVER].processes`` 的解析。"""

    def test_default_is_single_process(self, tmp_path: Any) -> None:
        """没写就是 1——多进程必须显式打开，不能升个版就默默改变部署形态。"""
        assert _resolve_processes(_config(tmp_path, "[SERVER]\nport = 9528\n"), None) == 1

    def test_explicit_count(self, tmp_path: Any) -> None:
        assert _resolve_processes(_config(tmp_path, "[SERVER]\nprocesses = 4\n"), None) == 4

    def test_zero_means_auto_and_is_capped(self, tmp_path: Any) -> None:
        """0 = 按 CPU 核数，但要有上限。

        每个进程都要一份完整的适配器与连接池，无上限地跟着核数走会在
        大机器上白白吃掉几个 GB。
        """
        count = _resolve_processes(_config(tmp_path, "[SERVER]\nprocesses = 0\n"), None)
        assert 1 <= count <= 8

    @pytest.mark.parametrize("value", ["-3", '"abc"', "false"])
    def test_garbage_falls_back_to_single(self, tmp_path: Any, value: str) -> None:
        """配错了退回单进程，而不是崩在启动路径上。"""
        assert _resolve_processes(_config(tmp_path, f"[SERVER]\nprocesses = {value}\n"), None) == 1


class TestConcurrencyAdmission:
    """准入上限与线程数解耦。"""

    @staticmethod
    def _server(monkeypatch: pytest.MonkeyPatch, config: dict[str, Any]) -> dict[str, Any]:
        """起一个只到 grpc.server(...) 为止的服务端，把参数截下来。"""
        captured: dict[str, Any] = {}

        def spy(_pool: Any, **kwargs: Any) -> Any:
            captured.update(kwargs)
            raise RuntimeError("stop-here")

        monkeypatch.setattr("ipclick.server.grpc.server", spy)
        server = IPClickServer.__new__(IPClickServer)
        server.config = Settings(config)  # type: ignore[assignment]
        server._reuseport = False
        server._host, server._port = "127.0.0.1", 9528
        server.recorder = type("R", (), {"node_id": "t"})()  # type: ignore[assignment]
        with pytest.raises(RuntimeError, match="stop-here"):
            server.start()
        return captured

    def test_default_is_eight_times_workers_not_two(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """默认 max_workers * 8。

        0.5.0 是 * 2，于是默认部署在 200 并发以上就开始拒流——实测 1000 并发
        时成功率掉到 25.8%，而服务端 CPU 只用了 1.45 个核。
        """
        captured = self._server(monkeypatch, {"SERVER": {"max_workers": 100}})
        assert captured["maximum_concurrent_rpcs"] == 800

    def test_explicit_value_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = self._server(monkeypatch, {"SERVER": {"max_workers": 50, "max_concurrent_rpcs": 4096}})
        assert captured["maximum_concurrent_rpcs"] == 4096

    def test_below_workers_is_rejected(self) -> None:
        """准入上限小于线程数是纯粹的浪费，直接拒绝而不是默默接受。"""
        server = IPClickServer.__new__(IPClickServer)
        server.config = Settings({"SERVER": {"max_workers": 100, "max_concurrent_rpcs": 10}})  # type: ignore[assignment]
        server._reuseport = False
        server._host, server._port = "127.0.0.1", 9528
        with pytest.raises(ConfigError, match="max_concurrent_rpcs"):
            server.start()

    def test_streams_follow_admission_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """单条 HTTP/2 连接的并发流上限不再写死 100。

        SDK 一个 Downloader 就是一条 channel / 一条 TCP 连接，写死 100 等于
        给单个客户端设了一个它无法绕过的并发天花板。
        """
        captured = self._server(monkeypatch, {"SERVER": {"max_workers": 100}})
        options = dict(captured["options"])
        assert options["grpc.max_concurrent_streams"] == 800

    def test_streams_never_below_100(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """小 worker 数也不该把单客户端并发压到 100 以下。"""
        captured = self._server(monkeypatch, {"SERVER": {"max_workers": 2}})
        options = dict(captured["options"])
        assert options["grpc.max_concurrent_streams"] >= 100


class TestCompressionOption:
    @pytest.mark.parametrize(
        ("configured", "expected"),
        [
            ({}, grpc.Compression.Gzip),
            ({"compression": "gzip"}, grpc.Compression.Gzip),
            ({"compression": "GZIP"}, grpc.Compression.Gzip),
            ({"compression": "deflate"}, grpc.Compression.Deflate),
            ({"compression": "none"}, grpc.Compression.NoCompression),
            ({"compression": "off"}, grpc.Compression.NoCompression),
            ({"compression": "什么鬼"}, grpc.Compression.Gzip),
        ],
    )
    def test_mapping(self, configured: dict[str, Any], expected: Any) -> None:
        """默认保持 gzip（与 0.5.0 一致），认不出来的值也回落到 gzip。"""
        assert IPClickServer._compression(configured) is expected


class TestReuseportGate:
    def test_off_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """单进程模式必须继续关死 SO_REUSEPORT。

        开着它，端口被别的程序占了也能"启动成功"——两个进程一起监听、
        请求被内核随机分走，症状极难定位。
        """
        captured = TestConcurrencyAdmission._server(monkeypatch, {"SERVER": {"max_workers": 4}})
        assert dict(captured["options"])["grpc.so_reuseport"] == 0

    def test_on_for_forked_workers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        def spy(_pool: Any, **kwargs: Any) -> Any:
            captured.update(kwargs)
            raise RuntimeError("stop-here")

        monkeypatch.setattr("ipclick.server.grpc.server", spy)
        server = IPClickServer.__new__(IPClickServer)
        server.config = Settings({"SERVER": {"max_workers": 4}})  # type: ignore[assignment]
        server._reuseport = True
        server._host, server._port = "127.0.0.1", 9528
        with pytest.raises(RuntimeError, match="stop-here"):
            server.start()
        assert dict(captured["options"])["grpc.so_reuseport"] == 1


class TestUserAgentPool:
    """``fake_useragent`` 曾在每请求热路径上，单次 2.82ms。"""

    @staticmethod
    def _adapter(values: list[str]) -> Any:
        from ipclick.adapters.base import DownloaderAdapter

        class Fake(DownloaderAdapter):
            adapter_name = "fake"

            def download(self, url: str, **kwargs: Any) -> Any:  # pragma: no cover - 不会被调用
                raise NotImplementedError

        adapter = Fake()
        calls = {"n": 0}

        class Gen:
            @property
            def random(self) -> str:
                calls["n"] += 1
                return values[calls["n"] % len(values)]

        adapter.ua_generator = Gen()  # type: ignore[attr-defined]
        adapter._calls = calls  # type: ignore[attr-defined]
        return adapter

    def test_generator_is_consulted_once_per_pool_not_per_request(self) -> None:
        """池子只建一次。取一万次 UA 不该再碰生成器一下。"""
        from ipclick.adapters.base import UA_POOL_SIZE

        adapter = self._adapter([f"UA-{i}" for i in range(16)])
        for _ in range(10_000):
            adapter._get_user_agent()
        assert adapter._calls["n"] == UA_POOL_SIZE  # type: ignore[attr-defined]

    def test_still_rotates(self) -> None:
        """池化不能把"轮换 UA"变成"固定 UA"——那会削弱反检测。"""
        adapter = self._adapter([f"UA-{i}" for i in range(16)])
        seen = {adapter._get_user_agent() for _ in range(500)}
        assert len(seen) > 1

    def test_falls_back_when_generator_missing(self) -> None:
        """没装 fake_useragent 时回落到内置 UA，而不是空串或异常。"""
        from ipclick.adapters.base import DownloaderAdapter

        class Fake(DownloaderAdapter):
            adapter_name = "fake"

            def download(self, url: str, **kwargs: Any) -> Any:  # pragma: no cover
                raise NotImplementedError

        adapter = Fake()
        assert adapter._get_user_agent() == adapter.user_agent

    def test_falls_back_when_generator_raises(self) -> None:
        from ipclick.adapters.base import DownloaderAdapter

        class Fake(DownloaderAdapter):
            adapter_name = "fake"

            def download(self, url: str, **kwargs: Any) -> Any:  # pragma: no cover
                raise NotImplementedError

        class Broken:
            @property
            def random(self) -> str:
                raise RuntimeError("数据文件坏了")

        adapter = Fake()
        adapter.ua_generator = Broken()  # type: ignore[attr-defined]
        assert adapter._get_user_agent() == adapter.user_agent


class TestUnavailableHint:
    """UNAVAILABLE 有两种成因，排查方向相反。"""

    def test_refused_stream_points_at_admission_limit(self) -> None:
        from ipclick.sdk import unavailable_hint

        hint = unavailable_hint("Stream removed (RST_STREAM with error code 7 REFUSED_STREAM)", 9528)
        assert "max_concurrent_rpcs" in hint
        assert "Web 管理端" not in hint

    def test_real_connect_failure_keeps_port_hint(self) -> None:
        """真连不上时才提端口——0.5.0 换过默认端口，那一句在这里才有用。"""
        from ipclick.ports import DEFAULT_WEB_PORT
        from ipclick.sdk import unavailable_hint

        hint = unavailable_hint("failed to connect to all addresses", DEFAULT_WEB_PORT)
        assert "Web 管理端" in hint

    def test_no_noise_for_ordinary_ports(self) -> None:
        from ipclick.sdk import unavailable_hint

        assert unavailable_hint("failed to connect to all addresses", 9528) == ""
