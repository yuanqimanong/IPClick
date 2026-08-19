from __future__ import annotations

from collections.abc import Iterator
from concurrent import futures

import grpc
import pytest

from ipclick.adapters import registry
from ipclick.auth import TokenAuthInterceptor
from ipclick.dto.models import DownloadTask, HttpMethod
from ipclick.dto.proto import task_pb2_grpc
from ipclick.exceptions import AuthenticationError
from ipclick.rpc import server_options
from ipclick.sdk import Downloader
from ipclick.services.task_service import TaskService
from ipclick.utils.config_util import Settings

from .helpers import StubAdapter


pytestmark = pytest.mark.slow

TOKEN = "roundtrip-token"


@pytest.fixture
def running_server(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[int, TaskService]]:
    monkeypatch.setitem(registry.ADAPTER_CLASSES, StubAdapter.adapter_name, StubAdapter)
    service = TaskService(settings)
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=2),
        interceptors=[TokenAuthInterceptor((TOKEN,))],
        options=server_options(max_concurrent_streams=100),
    )
    task_pb2_grpc.add_TaskServiceServicer_to_server(service, server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        yield port, service
    finally:
        server.stop(None).wait(timeout=5)
        service.cleanup()


def test_request_travels_end_to_end(running_server: tuple[int, TaskService]) -> None:
    port, service = running_server
    with Downloader(host="127.0.0.1", port=port, token=TOKEN) as client:
        response = client.get("http://127.0.0.1/probe")

    assert response.status_code == 200
    assert response.content == b"body"
    assert response.text == "body"
    assert response.adapter_type == "curl_cffi"
    assert response.trace.node_id == service.node_id
    assert response.trace.adapter == "curl_cffi"
    assert response.elapsed_ms >= 0
    assert response.ok


def test_missing_token_is_rejected_by_the_interceptor(running_server: tuple[int, TaskService]) -> None:
    port, _ = running_server
    with Downloader(host="127.0.0.1", port=port) as client, pytest.raises(AuthenticationError):
        client.download(DownloadTask(url="http://127.0.0.1/probe"))


def test_wrong_token_is_rejected(running_server: tuple[int, TaskService]) -> None:
    port, _ = running_server
    with Downloader(host="127.0.0.1", port=port, token="nope") as client, pytest.raises(AuthenticationError):
        client.download(DownloadTask(url="http://127.0.0.1/probe"))


def test_streaming_download_reassembles_the_body(running_server: tuple[int, TaskService]) -> None:
    port, _ = running_server
    with Downloader(host="127.0.0.1", port=port, token=TOKEN) as client:
        stream = client.stream("http://127.0.0.1/probe")
        assert stream.status_code == 200
        assert stream.read() == b"body"
        assert stream.total_bytes == 4
        stream.close()


def test_batch_returns_one_response_per_task(running_server: tuple[int, TaskService]) -> None:
    port, _ = running_server
    tasks = [DownloadTask(url=f"http://127.0.0.1/{index}", method=HttpMethod.GET) for index in range(5)]

    with Downloader(host="127.0.0.1", port=port, token=TOKEN) as client:
        responses = list(client.batch(tasks))

    assert len(responses) == 5
    assert {r.status_code for r in responses} == {200}


def test_client_refuses_to_work_after_close(running_server: tuple[int, TaskService]) -> None:
    port, _ = running_server
    client = Downloader(host="127.0.0.1", port=port, token=TOKEN)
    client.close()

    from ipclick.exceptions import ClientClosedError

    with pytest.raises(ClientClosedError):
        client.download(DownloadTask(url="http://127.0.0.1/probe"))
