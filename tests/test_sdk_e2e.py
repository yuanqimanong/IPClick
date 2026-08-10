"""SDK 端到端测试：起一个真实的 in-process gRPC 服务端，只把 HTTP 适配器换成假的。

覆盖 SDK -> protobuf -> 服务端 -> 适配器 的完整链路。
"""

from collections.abc import Iterator
from concurrent import futures
import socket
from typing import Any

import grpc
import pytest

from ipclick.adapters.base import DownloaderAdapter
from ipclick.dto.proto import task_pb2_grpc
from ipclick.dto.response import Response
from ipclick.exceptions import TransportError
from ipclick.sdk import Downloader
from ipclick.services.task_service import TaskService
from ipclick.utils.config_util import Settings


class EchoAdapter(DownloaderAdapter):
    """把收到的参数回显成响应体，供客户端断言。"""

    adapter_name = "curl_cffi"

    def __init__(self):
        super().__init__()
        self.received: list[dict[str, Any]] = []

    def download(self, url: str, **kwargs: Any) -> Response:  # type: ignore[override]
        self.received.append({"url": url, **kwargs})
        import json

        body = json.dumps({"url": url, "method": kwargs.get("method"), "verify": kwargs.get("verify")})
        return Response(url=url, status_code=200, content=body.encode(), headers={"Content-Type": "application/json"})


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def live_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[int, EchoAdapter]]:
    """启动一个真实的 gRPC 服务端，返回 (端口, 假适配器)。"""
    adapter = EchoAdapter()
    monkeypatch.setattr("ipclick.services.task_service.get_default_adapter", lambda settings=None: adapter)
    monkeypatch.setattr("ipclick.services.task_service.get_adapter", lambda name, settings=None: adapter)

    service = TaskService(Settings({"SECURITY": {"block_private_networks": False}}))
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4), maximum_concurrent_rpcs=8)
    task_pb2_grpc.add_TaskServiceServicer_to_server(service, server)

    port = _free_port()
    assert server.add_insecure_port(f"127.0.0.1:{port}") == port
    server.start()
    try:
        yield port, adapter
    finally:
        server.stop(grace=0).wait(timeout=5)
        service.cleanup()


@pytest.fixture
def client(live_server: tuple[int, EchoAdapter]) -> Iterator[Downloader]:
    port, _ = live_server
    with Downloader(host="127.0.0.1", port=port) as downloader:
        yield downloader


class TestEndToEnd:
    def test_get_round_trip(self, client: Downloader):
        resp = client.get("http://example.com/x")
        assert resp.status_code == 200
        assert resp.is_success()
        assert resp.json()["url"] == "http://example.com/x"
        assert resp.adapter_type == "curl_cffi"

    def test_ssl_verification_on_by_default_end_to_end(self, client: Downloader):
        """最关键的回归：默认调用链下 verify 必须一路是 True。"""
        resp = client.get("http://example.com/x")
        assert resp.json()["verify"] is True

    def test_verify_false_is_honoured(self, client: Downloader, live_server: tuple[int, EchoAdapter]):
        client.get("http://example.com/x", verify=False)
        assert live_server[1].received[-1]["verify"] is False

    def test_delete_forwards_url(self, client: Downloader, live_server: tuple[int, EchoAdapter]):
        """回归：delete() 以前不转发 url，必定 TypeError。"""
        resp = client.delete("http://example.com/gone")
        assert resp.status_code == 200
        assert live_server[1].received[-1]["url"] == "http://example.com/gone"
        assert live_server[1].received[-1]["method"] == "DELETE"

    @pytest.mark.parametrize(
        ("call", "expected_method"),
        [
            ("get", "GET"),
            ("post", "POST"),
            ("put", "PUT"),
            ("patch", "PATCH"),
            ("delete", "DELETE"),
            ("head", "HEAD"),
            ("options", "OPTIONS"),
        ],
    )
    def test_all_verbs_forward_url_and_method(
        self, client: Downloader, live_server: tuple[int, EchoAdapter], call: str, expected_method: str
    ):
        getattr(client, call)("http://example.com/verb")
        last = live_server[1].received[-1]
        assert last["url"] == "http://example.com/verb"
        assert last["method"] == expected_method

    def test_post_json_body_reaches_adapter(self, client: Downloader, live_server: tuple[int, EchoAdapter]):
        client.post("http://example.com/x", json={"ping": "pong"})
        assert live_server[1].received[-1]["json"] == {"ping": "pong"}

    def test_post_form_data_reaches_adapter(self, client: Downloader, live_server: tuple[int, EchoAdapter]):
        client.post("http://example.com/x", data={"foo": "bar"})
        assert live_server[1].received[-1]["data"] == {"foo": "bar"}

    def test_headers_and_cookies_forwarded(self, client: Downloader, live_server: tuple[int, EchoAdapter]):
        client.get("http://example.com/x", headers={"X-A": "1"}, cookies={"s": "abc"})
        last = live_server[1].received[-1]
        assert last["headers"]["X-A"] == "1"
        assert last["cookies"]["s"] == "abc"

    def test_allow_redirects_false_forwarded(self, client: Downloader, live_server: tuple[int, EchoAdapter]):
        client.get("http://example.com/x", allow_redirects=False)
        assert live_server[1].received[-1]["allow_redirects"] is False


class TestChannelReuse:
    def test_single_channel_across_requests(self, client: Downloader):
        """回归：以前每个请求新建一个 gRPC channel。"""
        client.get("http://example.com/1")
        first = client._channel
        client.get("http://example.com/2")
        assert client._channel is first is not None

    def test_close_is_idempotent(self, client: Downloader):
        client.get("http://example.com/1")
        client.close()
        client.close()
        assert client._channel is None

    def test_use_after_close_raises(self, client: Downloader):
        client.close()
        with pytest.raises(TransportError, match="已关闭"):
            client._get_stub()

    def test_context_manager_closes(self, live_server: tuple[int, EchoAdapter]):
        port, _ = live_server
        with Downloader(host="127.0.0.1", port=port) as d:
            d.get("http://example.com/x")
        assert d._channel is None


class TestTransportFailure:
    def test_unreachable_server_returns_error_response_not_none(self):
        """回归：request() 吞掉异常后隐式返回 None，而签名写的是 DownloadResponse。
        examples 里也是按"拿到 status_code == -1 的响应"来用的。"""
        with Downloader(host="127.0.0.1", port=_free_port()) as d:
            resp = d.get("http://example.com/x", max_retries=0, timeout=1)

        assert resp is not None
        assert resp.status_code == -1
        assert resp.error
        assert not resp.is_success()

    def test_typo_in_adapter_name_raises_instead_of_looking_like_a_network_error(self):
        """回归：request() 用 `except IPClickError` 兜底，而 ValidationError 是它的
        子类，于是适配器名拼错会返回 status_code == -1 的响应，看起来像网络故障，
        调用方对着网络排查半天也找不到原因。参数错误必须抛出。"""
        from ipclick.exceptions import ValidationError

        with Downloader(host="127.0.0.1", port=_free_port()) as d, pytest.raises(ValidationError, match="适配器"):
            d.get("http://example.com/x", adapter="htttpx", max_retries=0, timeout=1)

    def test_invalid_url_still_raises(self):
        from ipclick.exceptions import ValidationError

        with Downloader(host="127.0.0.1", port=_free_port()) as d, pytest.raises(ValidationError):
            d.get("", max_retries=0, timeout=1)

    def test_transport_failure_still_returns_a_response(self):
        """与上面相对：真正的传输失败仍然返回响应对象而不是抛异常。"""
        with Downloader(host="127.0.0.1", port=_free_port()) as d:
            resp = d.get("http://example.com/x", max_retries=0, timeout=1)
        assert resp.status_code == -1

    def test_download_raises_transport_error_directly(self):
        """低层 download() 仍然抛异常，方便调用方自行处理。"""
        from ipclick.dto.models import DownloadTask

        with Downloader(host="127.0.0.1", port=_free_port()) as d, pytest.raises(TransportError):
            d.download(DownloadTask(url="http://example.com/x", timeout=1, max_retries=0))


class TestServerSideSecurity:
    def test_blocked_url_surfaces_to_client(self, monkeypatch: pytest.MonkeyPatch):
        adapter = EchoAdapter()
        monkeypatch.setattr("ipclick.services.task_service.get_default_adapter", lambda settings=None: adapter)
        monkeypatch.setattr("ipclick.services.task_service.get_adapter", lambda name, settings=None: adapter)

        service = TaskService(Settings({"SECURITY": {"block_private_networks": True}}))
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
        task_pb2_grpc.add_TaskServiceServicer_to_server(service, server)
        port = _free_port()
        server.add_insecure_port(f"127.0.0.1:{port}")
        server.start()

        try:
            with Downloader(host="127.0.0.1", port=port) as d:
                resp = d.get("http://192.168.1.1/admin", max_retries=0)
            # 服务端设置了 PERMISSION_DENIED，客户端表现为一次传输失败
            assert resp.status_code == -1
            assert resp.error
            assert adapter.received == []
        finally:
            server.stop(grace=0).wait(timeout=5)
            service.cleanup()
