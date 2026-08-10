"""令牌鉴权：纯函数、拦截器、以及真实 gRPC 服务端上的端到端行为。"""

from collections.abc import Iterator
from concurrent import futures

import grpc
import pytest

from ipclick.auth import (
    AUTH_METADATA_KEY,
    AUTH_TOKEN_ENV,
    TokenAuthInterceptor,
    build_client_metadata,
    extract_token,
    is_exempt,
    load_tokens,
    token_matches,
)
from ipclick.dto.proto import task_pb2_grpc
from ipclick.exceptions import AuthenticationError, TransportError
from ipclick.sdk import Downloader
from ipclick.services.task_service import TaskService
from ipclick.utils.config_util import Settings
from tests.test_sdk_e2e import EchoAdapter, _free_port


class TestLoadTokens:
    def test_empty_by_default(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv(AUTH_TOKEN_ENV, raising=False)
        assert load_tokens({}) == ()
        assert load_tokens(None) == ()

    def test_reads_config_string(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv(AUTH_TOKEN_ENV, raising=False)
        assert load_tokens({"auth_token": "s3cret"}) == ("s3cret",)

    def test_reads_config_list_for_rotation(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv(AUTH_TOKEN_ENV, raising=False)
        assert load_tokens({"auth_token": ["new", "old"]}) == ("new", "old")

    def test_env_takes_precedence_and_both_are_valid(self, monkeypatch: pytest.MonkeyPatch):
        """环境变量排在前面，但配置文件里的令牌同样有效——方便滚动切换。"""
        monkeypatch.setenv(AUTH_TOKEN_ENV, "from-env")
        assert load_tokens({"auth_token": "from-file"}) == ("from-env", "from-file")

    def test_blank_values_ignored(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(AUTH_TOKEN_ENV, "   ")
        assert load_tokens({"auth_token": ["", "  ", "real"]}) == ("real",)

    def test_deduplicates(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(AUTH_TOKEN_ENV, "same")
        assert load_tokens({"auth_token": "same"}) == ("same",)


class TestExtractToken:
    def test_bearer_prefix(self):
        assert extract_token([(AUTH_METADATA_KEY, "Bearer abc")]) == "abc"

    def test_bearer_is_case_insensitive(self):
        assert extract_token([(AUTH_METADATA_KEY, "bearer abc")]) == "abc"

    def test_bare_token_accepted(self):
        """方便 grpcurl 之类的工具手动调试。"""
        assert extract_token([(AUTH_METADATA_KEY, "abc")]) == "abc"

    def test_bytes_value(self):
        assert extract_token([(AUTH_METADATA_KEY, b"Bearer abc")]) == "abc"

    def test_missing_metadata(self):
        assert extract_token(None) is None
        assert extract_token([]) is None
        assert extract_token([("other-key", "abc")]) is None

    def test_empty_token(self):
        assert extract_token([(AUTH_METADATA_KEY, "Bearer   ")]) is None


class TestTokenMatches:
    def test_matches_any_configured_token(self):
        assert token_matches("old", ["new", "old"])
        assert token_matches("new", ["new", "old"])

    def test_rejects_unknown(self):
        assert not token_matches("nope", ["new", "old"])

    def test_rejects_empty(self):
        assert not token_matches(None, ["x"])
        assert not token_matches("", ["x"])

    def test_no_tokens_means_no_match(self):
        assert not token_matches("anything", [])

    def test_prefix_is_not_enough(self):
        """避免用前缀就能通过（compare_digest 本身保证，这里锁住行为）。"""
        assert not token_matches("secret", ["secretlonger"])
        assert not token_matches("secretlonger", ["secret"])


class TestExemptions:
    def test_health_check_is_exempt(self):
        """编排系统探活通常拿不到密钥。"""
        assert is_exempt("/grpc.health.v1.Health/Check")
        assert is_exempt("/grpc.health.v1.Health/Watch")

    def test_task_service_is_not_exempt(self):
        assert not is_exempt("/task.TaskService/Send")


class TestInterceptorUnit:
    def test_disabled_when_no_tokens(self):
        assert TokenAuthInterceptor([]).enabled is False

    def test_enabled_with_tokens(self):
        assert TokenAuthInterceptor(["x"]).enabled is True

    def test_passes_through_when_disabled(self):
        interceptor = TokenAuthInterceptor([])
        sentinel = object()
        details = type("D", (), {"method": "/task.TaskService/Send", "invocation_metadata": ()})()
        assert interceptor.intercept_service(lambda _: sentinel, details) is sentinel  # type: ignore[arg-type,return-value]

    def test_rejects_bad_token(self):
        interceptor = TokenAuthInterceptor(["good"])
        sentinel = object()
        details = type(
            "D", (), {"method": "/task.TaskService/Send", "invocation_metadata": ((AUTH_METADATA_KEY, "Bearer bad"),)}
        )()
        assert interceptor.intercept_service(lambda _: sentinel, details) is not sentinel  # type: ignore[arg-type]

    def test_accepts_good_token(self):
        interceptor = TokenAuthInterceptor(["good"])
        sentinel = object()
        details = type(
            "D", (), {"method": "/task.TaskService/Send", "invocation_metadata": ((AUTH_METADATA_KEY, "Bearer good"),)}
        )()
        assert interceptor.intercept_service(lambda _: sentinel, details) is sentinel  # type: ignore[arg-type,return-value]


@pytest.fixture
def secured_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[int, EchoAdapter]]:
    """启动一个启用了鉴权的真实 gRPC 服务端。"""
    adapter = EchoAdapter()
    monkeypatch.setattr("ipclick.services.task_service.get_default_adapter", lambda settings=None: adapter)
    monkeypatch.setattr(
        "ipclick.services.task_service.get_adapter", lambda name, settings=None, browser_settings=None: adapter
    )

    service = TaskService(Settings({"SECURITY": {"block_private_networks": False}}))
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=4),
        interceptors=[TokenAuthInterceptor(["right-token", "rotating-old-token"])],
    )
    task_pb2_grpc.add_TaskServiceServicer_to_server(service, server)

    port = _free_port()
    server.add_insecure_port(f"127.0.0.1:{port}")
    server.start()
    try:
        yield port, adapter
    finally:
        server.stop(grace=0).wait(timeout=5)
        service.cleanup()


class TestEndToEndAuth:
    def test_correct_token_is_accepted(self, secured_server: tuple[int, EchoAdapter]):
        port, adapter = secured_server
        with Downloader(host="127.0.0.1", port=port, token="right-token") as d:
            resp = d.get("http://example.com/x")
        assert resp.status_code == 200
        assert len(adapter.received) == 1

    def test_rotating_old_token_still_works(self, secured_server: tuple[int, EchoAdapter]):
        """轮换期间新旧令牌都应有效，否则换密钥必须停机。"""
        port, _ = secured_server
        with Downloader(host="127.0.0.1", port=port, token="rotating-old-token") as d:
            assert d.get("http://example.com/x").status_code == 200

    def test_wrong_token_raises_authentication_error(self, secured_server: tuple[int, EchoAdapter]):
        """鉴权失败必须抛出，而不是伪装成 status_code == -1 的网络失败——
        令牌错了重试多少次都没用。"""
        port, adapter = secured_server
        with Downloader(host="127.0.0.1", port=port, token="wrong") as d, pytest.raises(AuthenticationError):
            d.get("http://example.com/x")
        assert adapter.received == [], "鉴权失败的请求不应到达适配器"

    def test_missing_token_raises_authentication_error(self, secured_server: tuple[int, EchoAdapter]):
        port, adapter = secured_server
        with Downloader(host="127.0.0.1", port=port, token=None) as d, pytest.raises(AuthenticationError):
            d.get("http://example.com/x")
        assert adapter.received == []

    def test_error_message_does_not_leak_the_token(self, secured_server: tuple[int, EchoAdapter]):
        port, _ = secured_server
        with Downloader(host="127.0.0.1", port=port, token="my-wrong-token") as d:
            try:
                d.get("http://example.com/x")
            except AuthenticationError as e:
                assert "my-wrong-token" not in str(e)
                assert "right-token" not in str(e)
            else:
                pytest.fail("应当抛出 AuthenticationError")

    def test_auth_error_is_not_a_transport_error(self, secured_server: tuple[int, EchoAdapter]):
        """刻意不继承 TransportError，否则会被 request() 吞成一次网络失败。"""
        port, _ = secured_server
        with Downloader(host="127.0.0.1", port=port, token="wrong") as d:
            try:
                d.get("http://example.com/x")
            except TransportError:
                pytest.fail("AuthenticationError 不应被当作 TransportError 捕获")
            except AuthenticationError:
                pass


class TestTokenSourceResolution:
    def test_client_reads_env_token(self, secured_server: tuple[int, EchoAdapter], monkeypatch: pytest.MonkeyPatch):
        from ipclick.config_loader.loader import load_config

        port, _ = secured_server
        monkeypatch.setenv(AUTH_TOKEN_ENV, "right-token")
        load_config.cache_clear()
        with Downloader(host="127.0.0.1", port=port) as d:
            assert d.get("http://example.com/x").status_code == 200

    def test_explicit_token_overrides_env(
        self, secured_server: tuple[int, EchoAdapter], monkeypatch: pytest.MonkeyPatch
    ):
        port, _ = secured_server
        monkeypatch.setenv(AUTH_TOKEN_ENV, "wrong-from-env")
        with Downloader(host="127.0.0.1", port=port, token="right-token") as d:
            assert d.get("http://example.com/x").status_code == 200


class TestBackwardsCompatibility:
    def test_server_without_tokens_accepts_everyone(self, monkeypatch: pytest.MonkeyPatch):
        """未配置令牌时保持放行，避免现有部署升级即中断（启动时会打告警）。"""
        adapter = EchoAdapter()
        monkeypatch.setattr("ipclick.services.task_service.get_default_adapter", lambda settings=None: adapter)
        monkeypatch.setattr(
            "ipclick.services.task_service.get_adapter", lambda name, settings=None, browser_settings=None: adapter
        )

        service = TaskService(Settings({}))
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=2), interceptors=[TokenAuthInterceptor([])])
        task_pb2_grpc.add_TaskServiceServicer_to_server(service, server)
        port = _free_port()
        server.add_insecure_port(f"127.0.0.1:{port}")
        server.start()
        try:
            with Downloader(host="127.0.0.1", port=port) as d:
                assert d.get("http://example.com/x").status_code == 200
        finally:
            server.stop(grace=0).wait(timeout=5)
            service.cleanup()

    def test_metadata_empty_without_token(self):
        assert build_client_metadata(None) == ()
        assert build_client_metadata("") == ()

    def test_metadata_uses_bearer(self):
        assert build_client_metadata("abc") == ((AUTH_METADATA_KEY, "Bearer abc"),)
