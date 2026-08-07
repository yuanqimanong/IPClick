"""IPClickServer 的启动参数与停机行为。"""

import socket
from typing import Any

import pytest

from ipclick.exceptions import ConfigError
from ipclick.server import IPClickServer


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def captured_pool(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """记录 ThreadPoolExecutor 实际拿到的 max_workers。"""
    captured: dict[str, Any] = {}
    real = __import__("concurrent.futures", fromlist=["ThreadPoolExecutor"]).ThreadPoolExecutor

    def spy(*args: Any, **kwargs: Any):
        captured.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr("ipclick.server.futures.ThreadPoolExecutor", spy)
    return captured


class TestMaxWorkers:
    def test_port_override_does_not_become_max_workers(
        self, captured_pool: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        """核心回归：`max_workers = port or ...` 让 --port 9527 建出 9527 个线程。"""
        cfg = tmp_path / "c.toml"
        cfg.write_text('[SERVER]\nhost = "127.0.0.1"\nmax_workers = 8\n', encoding="utf-8")

        server = IPClickServer(str(cfg))
        port = _free_port()
        # wait_for_termination 会阻塞，替换掉以便测试启动逻辑
        monkeypatch.setattr("grpc._server._Server.wait_for_termination", lambda self, timeout=None: True)

        try:
            server.start(host="127.0.0.1", port=port)
        finally:
            server.stop(grace_period=0)

        assert captured_pool["max_workers"] == 8
        assert captured_pool["max_workers"] != port

    def test_invalid_max_workers_rejected(self, tmp_path):
        cfg = tmp_path / "c.toml"
        cfg.write_text('[SERVER]\nhost = "127.0.0.1"\nmax_workers = 0\n', encoding="utf-8")

        server = IPClickServer(str(cfg))
        with pytest.raises(ConfigError, match="max_workers"):
            server.start(host="127.0.0.1", port=_free_port())


class TestBinding:
    def test_bind_failure_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        """绑定失败时必须报错，而不是"启动成功"但没在监听。"""
        cfg = tmp_path / "c.toml"
        cfg.write_text("[SERVER]\nmax_workers = 2\n", encoding="utf-8")

        # 占住一个端口，再让服务端去绑同一个
        holder = socket.socket()
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        taken_port = holder.getsockname()[1]

        server = IPClickServer(str(cfg))
        try:
            with pytest.raises(RuntimeError, match="Failed to bind"):
                server.start(host="127.0.0.1", port=taken_port)
        finally:
            holder.close()


class TestShutdown:
    def test_stop_is_idempotent(self, tmp_path):
        cfg = tmp_path / "c.toml"
        cfg.write_text("[SERVER]\nmax_workers = 2\n", encoding="utf-8")
        server = IPClickServer(str(cfg))
        server.stop()
        server.stop()

    def test_stop_waits_for_termination(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        """回归：server.stop() 返回的是 Event，原代码没 wait 就 sys.exit，
        在途请求会被直接掐断。"""
        cfg = tmp_path / "c.toml"
        cfg.write_text('[SERVER]\nhost = "127.0.0.1"\nmax_workers = 2\n', encoding="utf-8")

        server = IPClickServer(str(cfg))
        monkeypatch.setattr("grpc._server._Server.wait_for_termination", lambda self, timeout=None: True)
        server.start(host="127.0.0.1", port=_free_port())

        waited: list[bool] = []
        real_stop = server.server.stop  # type: ignore[union-attr]

        def spy_stop(grace: float):
            event = real_stop(grace)
            original_wait = event.wait

            def wrapped_wait(timeout: float | None = None):
                waited.append(True)
                return original_wait(timeout)

            event.wait = wrapped_wait  # type: ignore[method-assign]
            return event

        server.server.stop = spy_stop  # type: ignore[union-attr]
        server.stop(grace_period=0)

        assert waited, "stop() 必须等待 gRPC 停机事件"
        assert server.server is None
