"""流式下载（SendStream）与批量（SendBatch）。

都在真实的 in-process gRPC 服务端上跑，只把 HTTP 适配器换成假的。
"""

from collections.abc import Iterator
from concurrent import futures
import time
from typing import Any

import grpc
import pytest

from ipclick.adapters.base import DEFAULT_CHUNK_SIZE, DownloaderAdapter, StreamEvent, StreamHeader
from ipclick.dto.models import DownloadTask
from ipclick.dto.proto import task_pb2_grpc
from ipclick.dto.response import Response
from ipclick.exceptions import TransportError
from ipclick.sdk import Downloader
from ipclick.services.task_service import TaskService
from ipclick.utils.config_util import Settings
from tests.test_sdk_e2e import _free_port


#: 用一个明显大于单个分片的响应体，确保真的分了多片
BIG_BODY = b"".join(f"line-{i:06d}\n".encode() for i in range(20000))


class StreamingAdapter(DownloaderAdapter):
    """产出可控分片的假适配器。"""

    adapter_name = "curl_cffi"

    def __init__(self, body: bytes = BIG_BODY, fail_with: str | None = None, delay: float = 0.0):
        super().__init__()
        self.body = body
        self.fail_with = fail_with
        self.delay = delay
        self.stream_calls = 0
        self.download_calls = 0

    def download(self, url: str, **kwargs: Any) -> Response:  # type: ignore[override]
        self.download_calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.fail_with:
            return Response.error_response(url, RuntimeError(self.fail_with))
        return Response(url=url, status_code=200, content=self.body, headers={"X-Test": "1"})

    def download_stream(  # type: ignore[override]
        self, url: str, *, chunk_size: int = DEFAULT_CHUNK_SIZE, **kwargs: Any
    ) -> Iterator[StreamEvent]:
        self.stream_calls += 1
        if self.fail_with:
            yield StreamHeader(url=url, status_code=-1, error=self.fail_with)
            return
        yield StreamHeader(
            url=url,
            status_code=200,
            headers={"X-Test": "1", "content-length": str(len(self.body))},
            content_length=len(self.body),
        )
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start : start + chunk_size]


def _start_server(adapter: DownloaderAdapter, monkeypatch: pytest.MonkeyPatch, config: dict[str, Any] | None = None):
    monkeypatch.setattr("ipclick.services.task_service.get_default_adapter", lambda settings=None: adapter)
    monkeypatch.setattr(
        "ipclick.services.task_service.get_adapter", lambda name, settings=None, browser_settings=None: adapter
    )
    service = TaskService(Settings(config or {}))
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8), maximum_concurrent_rpcs=16)
    task_pb2_grpc.add_TaskServiceServicer_to_server(service, server)
    port = _free_port()
    server.add_insecure_port(f"127.0.0.1:{port}")
    server.start()
    return server, service, port


@pytest.fixture
def stream_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[int, StreamingAdapter]]:
    adapter = StreamingAdapter()
    server, service, port = _start_server(adapter, monkeypatch)
    try:
        yield port, adapter
    finally:
        server.stop(grace=0).wait(timeout=5)
        service.cleanup()


class TestStreamDownload:
    def test_header_available_before_body(self, stream_server: tuple[int, StreamingAdapter]):
        """流式的核心价值：拿到状态码和响应头时还没开始收 body。"""
        port, _ = stream_server
        with Downloader(host="127.0.0.1", port=port) as d, d.stream("http://example.com/big") as resp:
            assert resp.status_code == 200
            assert resp.headers["X-Test"] == "1"
            assert resp.content_length == len(BIG_BODY)
            assert resp.is_success()

    def test_body_reassembles_exactly(self, stream_server: tuple[int, StreamingAdapter]):
        port, _ = stream_server
        with Downloader(host="127.0.0.1", port=port) as d, d.stream("http://example.com/big") as resp:
            received = b"".join(resp)
        assert received == BIG_BODY

    def test_actually_arrives_in_multiple_chunks(self, stream_server: tuple[int, StreamingAdapter]):
        """否则就只是把整体传输包装了一层，没有真的流式。"""
        port, _ = stream_server
        with Downloader(host="127.0.0.1", port=port) as d, d.stream("http://example.com/big") as resp:
            chunks = list(resp)
        assert len(chunks) > 1, f"只收到 {len(chunks)} 个分片，没有真正分片传输"
        assert sum(len(c) for c in chunks) == len(BIG_BODY)

    def test_uses_stream_path_not_download(self, stream_server: tuple[int, StreamingAdapter]):
        """确认服务端走的是 download_stream 而不是退回整体下载。"""
        port, adapter = stream_server
        with Downloader(host="127.0.0.1", port=port) as d, d.stream("http://example.com/big") as resp:
            resp.read()
        assert adapter.stream_calls == 1
        assert adapter.download_calls == 0

    def test_trailer_reports_stats(self, stream_server: tuple[int, StreamingAdapter]):
        port, _ = stream_server
        with Downloader(host="127.0.0.1", port=port) as d, d.stream("http://example.com/big") as resp:
            resp.read()
            assert resp.total_bytes == len(BIG_BODY)
            assert resp.elapsed_ms >= 0
            assert resp.trailer_error is None

    def test_read_helper(self, stream_server: tuple[int, StreamingAdapter]):
        port, _ = stream_server
        with Downloader(host="127.0.0.1", port=port) as d, d.stream("http://example.com/big") as resp:
            assert resp.read() == BIG_BODY

    def test_early_close_does_not_hang(self, stream_server: tuple[int, StreamingAdapter]):
        """只看 header 就放弃，应当能干净退出（并 cancel 掉服务端的传输）。"""
        port, _ = stream_server
        with Downloader(host="127.0.0.1", port=port) as d, d.stream("http://example.com/big") as resp:
            assert resp.status_code == 200
            # 退出 with 即 close()

    def test_second_iteration_is_empty(self, stream_server: tuple[int, StreamingAdapter]):
        """流只能消费一次，第二次迭代不应该卡住或重复给数据。"""
        port, _ = stream_server
        with Downloader(host="127.0.0.1", port=port) as d, d.stream("http://example.com/big") as resp:
            first = resp.read()
            second = resp.read()
        assert first == BIG_BODY
        assert second == b""


class TestStreamErrors:
    def test_adapter_failure_surfaces_in_header(self, monkeypatch: pytest.MonkeyPatch):
        adapter = StreamingAdapter(fail_with="连接被拒绝")
        server, service, port = _start_server(adapter, monkeypatch)
        try:
            with Downloader(host="127.0.0.1", port=port) as d, d.stream("http://example.com/x") as resp:
                assert resp.status_code == -1
                assert resp.error is not None
                assert "连接被拒绝" in resp.error
                assert not resp.is_success()
                assert resp.read() == b""
        finally:
            server.stop(grace=0).wait(timeout=5)
            service.cleanup()

    def test_blocked_url_surfaces_in_header(self, monkeypatch: pytest.MonkeyPatch):
        """SSRF 拦截在流式路径上同样生效。"""
        adapter = StreamingAdapter()
        server, service, port = _start_server(adapter, monkeypatch, {"SECURITY": {"block_private_networks": True}})
        try:
            with Downloader(host="127.0.0.1", port=port) as d:
                try:
                    with d.stream("http://192.168.1.1/admin") as resp:
                        assert resp.status_code == -1
                        assert resp.error
                except TransportError:
                    pass  # 服务端设了 PERMISSION_DENIED，客户端侧表现为传输失败也可接受
            assert adapter.stream_calls == 0
        finally:
            server.stop(grace=0).wait(timeout=5)
            service.cleanup()

    def test_unreachable_server(self):
        with Downloader(host="127.0.0.1", port=_free_port()) as d, pytest.raises(TransportError):
            d.stream("http://example.com/x", timeout=2, max_retries=0)


class TestFallbackStreamImplementation:
    def test_base_class_fallback_splits_content(self):
        """未覆写 download_stream 的适配器走基类回退实现，接口一致。"""

        class PlainAdapter(DownloaderAdapter):
            adapter_name = "plain"

            def download(self, url: str, **kwargs: Any) -> Response:  # type: ignore[override]
                return Response(url=url, status_code=200, content=b"x" * 1000, headers={})

        events = list(PlainAdapter().download_stream("http://x", chunk_size=256))
        header = events[0]
        assert isinstance(header, StreamHeader)
        assert header.status_code == 200
        chunks = events[1:]
        assert len(chunks) == 4
        assert b"".join(chunks) == b"x" * 1000  # type: ignore[arg-type]


class TestBatch:
    def test_all_tasks_return(self, stream_server: tuple[int, StreamingAdapter]):
        port, _ = stream_server
        tasks = [DownloadTask(url=f"http://example.com/{i}") for i in range(8)]
        with Downloader(host="127.0.0.1", port=port) as d:
            responses = list(d.batch(tasks))
        assert len(responses) == 8
        assert all(r.status_code == 200 for r in responses)

    def test_single_task_batch(self, stream_server: tuple[int, StreamingAdapter]):
        port, _ = stream_server
        with Downloader(host="127.0.0.1", port=port) as d:
            responses = list(d.batch([DownloadTask(url="http://example.com/only")]))
        assert len(responses) == 1

    def test_empty_batch(self, stream_server: tuple[int, StreamingAdapter]):
        port, _ = stream_server
        with Downloader(host="127.0.0.1", port=port) as d:
            assert list(d.batch([])) == []

    def test_uuids_all_present(self, stream_server: tuple[int, StreamingAdapter]):
        """结果按完成顺序返回，所以要靠 uuid 对应回请求，不能靠顺序。"""
        port, _ = stream_server
        tasks = [DownloadTask(uuid=f"task-{i}", url=f"http://example.com/{i}") for i in range(6)]
        with Downloader(host="127.0.0.1", port=port) as d:
            got = {r.request_uuid for r in d.batch(tasks)}
        assert got == {f"task-{i}" for i in range(6)}

    def test_one_bad_task_does_not_kill_the_batch(self, monkeypatch: pytest.MonkeyPatch):
        """回归：Send 出错时会 set_code，若共用批量的 context，
        一个被 SSRF 拦截的任务就会把整条批量流标成 PERMISSION_DENIED，
        其余任务的结果全部丢失。"""
        adapter = StreamingAdapter()
        server, service, port = _start_server(adapter, monkeypatch, {"SECURITY": {"block_private_networks": True}})
        try:
            tasks = [
                DownloadTask(uuid="ok-1", url="http://example.com/1"),
                DownloadTask(uuid="bad", url="http://192.168.1.1/admin"),
                DownloadTask(uuid="ok-2", url="http://example.com/2"),
            ]
            with Downloader(host="127.0.0.1", port=port) as d:
                by_uuid = {r.request_uuid: r for r in d.batch(tasks)}

            assert set(by_uuid) == {"ok-1", "bad", "ok-2"}, "被拦截的任务把整个批次搞挂了"
            assert by_uuid["ok-1"].status_code == 200
            assert by_uuid["ok-2"].status_code == 200
            assert by_uuid["bad"].status_code == -1
            assert by_uuid["bad"].error
        finally:
            server.stop(grace=0).wait(timeout=5)
            service.cleanup()

    def test_results_stream_out_by_completion_not_submission(self, monkeypatch: pytest.MonkeyPatch):
        """并发执行：8 个各 0.2s 的任务，总耗时应远小于串行的 1.6s。"""
        adapter = StreamingAdapter(delay=0.2)
        server, service, port = _start_server(adapter, monkeypatch, {"SERVER": {"max_workers": 8}})
        try:
            tasks = [DownloadTask(url=f"http://example.com/{i}") for i in range(8)]
            with Downloader(host="127.0.0.1", port=port) as d:
                start = time.monotonic()
                responses = list(d.batch(tasks))
                elapsed = time.monotonic() - start

            assert len(responses) == 8
            assert elapsed < 1.0, f"批量耗时 {elapsed:.2f}s，看起来是串行执行的"
        finally:
            server.stop(grace=0).wait(timeout=5)
            service.cleanup()

    def test_batch_respects_auth(self, monkeypatch: pytest.MonkeyPatch):
        from ipclick.auth import TokenAuthInterceptor
        from ipclick.exceptions import AuthenticationError

        adapter = StreamingAdapter()
        monkeypatch.setattr("ipclick.services.task_service.get_default_adapter", lambda settings=None: adapter)
        monkeypatch.setattr(
            "ipclick.services.task_service.get_adapter", lambda name, settings=None, browser_settings=None: adapter
        )
        service = TaskService(Settings({}))
        server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=4),
            interceptors=[TokenAuthInterceptor(["good"])],
        )
        task_pb2_grpc.add_TaskServiceServicer_to_server(service, server)
        port = _free_port()
        server.add_insecure_port(f"127.0.0.1:{port}")
        server.start()
        try:
            with Downloader(host="127.0.0.1", port=port, token="bad") as d, pytest.raises(AuthenticationError):
                list(d.batch([DownloadTask(url="http://example.com/x")]))
        finally:
            server.stop(grace=0).wait(timeout=5)
            service.cleanup()
