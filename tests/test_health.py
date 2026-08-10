"""gRPC 标准健康检查（grpc.health.v1）。"""

from collections.abc import Iterator
from concurrent import futures
import socket

from click.testing import CliRunner
import grpc
from grpc_health.v1 import health_pb2, health_pb2_grpc
import pytest

from ipclick.auth import TokenAuthInterceptor
from ipclick.cli.main import main
from ipclick.dto.proto import task_pb2_grpc
from ipclick.health import (
    NOT_SERVING,
    OVERALL_SERVICE,
    SERVING,
    TASK_SERVICE_NAME,
    HealthReporter,
    check_health,
)
from ipclick.services.task_service import TaskService
from ipclick.utils.config_util import Settings
from tests.test_sdk_e2e import EchoAdapter, _free_port


@pytest.fixture
def health_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[int, HealthReporter]]:
    """启动一个注册了健康检查、且启用了鉴权的服务端。"""
    adapter = EchoAdapter()
    monkeypatch.setattr("ipclick.services.task_service.get_default_adapter", lambda settings=None: adapter)
    monkeypatch.setattr("ipclick.services.task_service.get_adapter", lambda name, settings=None: adapter)

    service = TaskService(Settings({}))
    reporter = HealthReporter(enabled=True)
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=4),
        interceptors=[TokenAuthInterceptor(["the-token"])],
    )
    task_pb2_grpc.add_TaskServiceServicer_to_server(service, server)
    reporter.register(server)

    port = _free_port()
    server.add_insecure_port(f"127.0.0.1:{port}")
    server.start()
    reporter.set_serving()
    try:
        yield port, reporter
    finally:
        server.stop(grace=0).wait(timeout=5)
        service.cleanup()


class TestServingStatus:
    def test_overall_status_is_serving(self, health_server: tuple[int, HealthReporter]):
        port, _ = health_server
        healthy, status = check_health(f"127.0.0.1:{port}")
        assert healthy
        assert status == "SERVING"

    def test_named_service_is_serving(self, health_server: tuple[int, HealthReporter]):
        port, _ = health_server
        healthy, status = check_health(f"127.0.0.1:{port}", service=TASK_SERVICE_NAME)
        assert healthy
        assert status == "SERVING"

    def test_unknown_service_reports_service_unknown(self, health_server: tuple[int, HealthReporter]):
        port, _ = health_server
        healthy, status = check_health(f"127.0.0.1:{port}", service="no.such.Service")
        assert not healthy
        assert "NOT_FOUND" in status or "SERVICE_UNKNOWN" in status

    def test_not_serving_after_graceful_shutdown(self, health_server: tuple[int, HealthReporter]):
        """停机第一步应先摘流量，此时端口还在监听但状态必须是 NOT_SERVING。"""
        port, reporter = health_server
        reporter.enter_graceful_shutdown()

        healthy, status = check_health(f"127.0.0.1:{port}")
        assert not healthy
        assert status == "NOT_SERVING"

    def test_set_not_serving_then_serving(self):
        reporter = HealthReporter(enabled=True)
        reporter.set_not_serving()
        reporter.set_serving()  # 未进入 graceful shutdown 时可以来回切


class TestAuthExemption:
    def test_health_check_does_not_require_token(self, health_server: tuple[int, HealthReporter]):
        """服务端启用了鉴权，但健康检查必须免鉴权——
        编排系统探活通常拿不到密钥。"""
        port, _ = health_server
        healthy, status = check_health(f"127.0.0.1:{port}")
        assert healthy, f"健康检查被鉴权挡住了: {status}"

    def test_business_rpc_still_requires_token(self, health_server: tuple[int, HealthReporter]):
        """反过来确认鉴权确实是开着的，否则上一条测试没有意义。"""
        from ipclick.exceptions import AuthenticationError
        from ipclick.sdk import Downloader

        port, _ = health_server
        with Downloader(host="127.0.0.1", port=port, token=None) as d, pytest.raises(AuthenticationError):
            d.get("http://example.com/x")


class TestDisabled:
    def test_not_registered_when_disabled(self, monkeypatch: pytest.MonkeyPatch):
        """[MONITOR].health_check = false 时不注册该服务。"""
        adapter = EchoAdapter()
        monkeypatch.setattr("ipclick.services.task_service.get_default_adapter", lambda settings=None: adapter)
        monkeypatch.setattr("ipclick.services.task_service.get_adapter", lambda name, settings=None: adapter)

        service = TaskService(Settings({}))
        reporter = HealthReporter(enabled=False)
        assert reporter.servicer is None

        server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
        task_pb2_grpc.add_TaskServiceServicer_to_server(service, server)
        reporter.register(server)  # 应为 no-op
        port = _free_port()
        server.add_insecure_port(f"127.0.0.1:{port}")
        server.start()
        try:
            healthy, status = check_health(f"127.0.0.1:{port}", timeout=3)
            assert not healthy
            assert "UNIMPLEMENTED" in status
        finally:
            server.stop(grace=0).wait(timeout=5)
            service.cleanup()

    def test_state_changes_are_noop_when_disabled(self):
        reporter = HealthReporter(enabled=False)
        reporter.set_serving()
        reporter.set_not_serving()
        reporter.enter_graceful_shutdown()  # 都不应抛异常


class TestCheckHealthErrorHandling:
    def test_unreachable_target(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            dead = int(s.getsockname()[1])
        healthy, status = check_health(f"127.0.0.1:{dead}", timeout=2)
        assert not healthy
        assert "UNAVAILABLE" in status

    def test_never_raises(self):
        """探活函数不应把异常抛给调用方——P4 的集群探活会大量调用它。"""
        healthy, status = check_health("no-such-host.invalid:1", timeout=2)
        assert not healthy
        assert status


class TestHealthCli:
    def test_exit_code_zero_when_healthy(self, health_server: tuple[int, HealthReporter]):
        port, _ = health_server
        result = CliRunner().invoke(main, ["health", "--port", str(port), "--timeout", "3"])
        assert result.exit_code == 0, result.output
        assert "SERVING" in result.output

    def test_exit_code_one_when_unhealthy(self):
        """Docker HEALTHCHECK / 就绪探针靠退出码判断。"""
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            dead = int(s.getsockname()[1])
        result = CliRunner().invoke(main, ["health", "--port", str(dead), "--timeout", "2"])
        assert result.exit_code == 1

    def test_exit_code_one_after_shutdown(self, health_server: tuple[int, HealthReporter]):
        port, reporter = health_server
        reporter.enter_graceful_shutdown()
        result = CliRunner().invoke(main, ["health", "--port", str(port), "--timeout", "3"])
        assert result.exit_code == 1
        assert "NOT_SERVING" in result.output


class TestConstants:
    def test_overall_service_is_empty_string(self):
        """gRPC 约定：空字符串代表整个服务器，kubelet 探针查的就是它。"""
        assert OVERALL_SERVICE == ""

    def test_task_service_name_matches_proto(self):
        """服务名必须和 task.proto 的 package + service 一致，否则查不到。"""
        assert TASK_SERVICE_NAME == "task.TaskService"
        descriptor = task_pb2_grpc.TaskServiceServicer
        assert descriptor is not None

    def test_status_constants(self):
        assert SERVING == health_pb2.HealthCheckResponse.SERVING
        assert NOT_SERVING == health_pb2.HealthCheckResponse.NOT_SERVING


class TestWatchStream:
    def test_watch_reports_status_change(self, health_server: tuple[int, HealthReporter]):
        """Watch 是流式的，服务网格用它做实时摘挂。

        注意必须显式 enable_http_proxy=0：gRPC 也会读环境里的 http_proxy，
        本机测试会被路由到环境代理去（check_health 里已经这么设了）。
        """
        port, reporter = health_server
        with grpc.insecure_channel(f"127.0.0.1:{port}", options=[("grpc.enable_http_proxy", 0)]) as channel:
            stub = health_pb2_grpc.HealthStub(channel)
            stream = stub.Watch(health_pb2.HealthCheckRequest(service=OVERALL_SERVICE))

            first = next(stream)
            assert first.status == SERVING

            reporter.enter_graceful_shutdown()
            second = next(stream)
            assert second.status == NOT_SERVING

            stream.cancel()
