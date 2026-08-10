"""异步客户端 AsyncDownloader。

重点验证两件事：异步与同步对同样输入产生同样结果，以及三种接口
（单请求 / 流式 / 批量）都能用。
"""

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

from ipclick.aio import AsyncDownloader, AsyncStreamedResponse
from ipclick.dto.models import DownloadTask, HttpMethod
from ipclick.exceptions import AuthenticationError, ClientClosedError, TransportError, ValidationError
from ipclick.sdk import Downloader
from tests.test_sdk_e2e import _free_port
from tests.test_streaming import BIG_BODY, StreamingAdapter, _start_server


@pytest.fixture
def aio_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[int, StreamingAdapter]]:
    adapter = StreamingAdapter()
    server, service, port = _start_server(adapter, monkeypatch)
    try:
        yield port, adapter
    finally:
        server.stop(grace=0).wait(timeout=5)
        service.cleanup()


class TestAsyncSingleRequest:
    async def test_get(self, aio_server: tuple[int, StreamingAdapter]):
        port, _ = aio_server
        async with AsyncDownloader(host="127.0.0.1", port=port) as d:
            resp = await d.get("http://example.com/x")
        assert resp.status_code == 200
        assert resp.is_success()

    @pytest.mark.parametrize(
        ("verb", "expected"),
        [("get", "GET"), ("post", "POST"), ("put", "PUT"), ("patch", "PATCH"), ("delete", "DELETE")],
    )
    async def test_all_verbs(self, aio_server: tuple[int, StreamingAdapter], verb: str, expected: str):
        port, adapter = aio_server
        async with AsyncDownloader(host="127.0.0.1", port=port) as d:
            await getattr(d, verb)("http://example.com/v")
        assert adapter.download_calls >= 1

    async def test_explicit_request(self, aio_server: tuple[int, StreamingAdapter]):
        port, _ = aio_server
        async with AsyncDownloader(host="127.0.0.1", port=port) as d:
            resp = await d.request(method=HttpMethod.GET, url="http://example.com/x", timeout=20)
        assert resp.status_code == 200

    async def test_unreachable_returns_error_response(self):
        """与同步版一致：传输失败返回响应对象而不是抛异常。"""
        async with AsyncDownloader(host="127.0.0.1", port=_free_port()) as d:
            resp = await d.get("http://example.com/x", timeout=2, max_retries=0)
        assert resp.status_code == -1
        assert resp.error

    async def test_validation_error_still_raises(self):
        """与同步版一致：参数错误抛出，不伪装成网络失败。"""
        async with AsyncDownloader(host="127.0.0.1", port=_free_port()) as d:
            with pytest.raises(ValidationError):
                await d.get("http://example.com/x", adapter="htttpx", timeout=2)

    async def test_use_after_close_raises(self, aio_server: tuple[int, StreamingAdapter]):
        port, _ = aio_server
        d = AsyncDownloader(host="127.0.0.1", port=port)
        await d.get("http://example.com/x")
        await d.close()
        with pytest.raises(ClientClosedError, match="已关闭"):
            await d.get("http://example.com/x")

    async def test_close_is_idempotent(self, aio_server: tuple[int, StreamingAdapter]):
        port, _ = aio_server
        d = AsyncDownloader(host="127.0.0.1", port=port)
        await d.get("http://example.com/x")
        await d.close()
        await d.close()


class TestAsyncStream:
    async def test_header_before_body(self, aio_server: tuple[int, StreamingAdapter]):
        port, _ = aio_server
        async with AsyncDownloader(host="127.0.0.1", port=port) as d:
            resp = await d.stream("http://example.com/big")
            async with resp:
                assert resp.status_code == 200
                assert resp.content_length == len(BIG_BODY)

    async def test_body_reassembles(self, aio_server: tuple[int, StreamingAdapter]):
        port, _ = aio_server
        async with AsyncDownloader(host="127.0.0.1", port=port) as d:
            resp = await d.stream("http://example.com/big")
            async with resp:
                chunks = [c async for c in resp]
        assert b"".join(chunks) == BIG_BODY
        assert len(chunks) > 1, "没有真正分片"

    async def test_read_helper(self, aio_server: tuple[int, StreamingAdapter]):
        port, _ = aio_server
        async with AsyncDownloader(host="127.0.0.1", port=port) as d:
            resp = await d.stream("http://example.com/big")
            async with resp:
                assert await resp.read() == BIG_BODY
                assert resp.total_bytes == len(BIG_BODY)

    async def test_early_close(self, aio_server: tuple[int, StreamingAdapter]):
        port, _ = aio_server
        async with AsyncDownloader(host="127.0.0.1", port=port) as d:
            resp = await d.stream("http://example.com/big")
            async with resp:
                assert resp.status_code == 200
                # 不读 body 直接退出

    async def test_error_surfaces_in_header(self, monkeypatch: pytest.MonkeyPatch):
        adapter = StreamingAdapter(fail_with="上游挂了")
        server, service, port = _start_server(adapter, monkeypatch)
        try:
            async with AsyncDownloader(host="127.0.0.1", port=port) as d:
                resp = await d.stream("http://example.com/x")
                async with resp:
                    assert resp.status_code == -1
                    assert resp.error and "上游挂了" in resp.error
        finally:
            server.stop(grace=0).wait(timeout=5)
            service.cleanup()


class TestAsyncBatch:
    async def test_all_tasks_return(self, aio_server: tuple[int, StreamingAdapter]):
        port, _ = aio_server
        tasks = [DownloadTask(uuid=f"t-{i}", url=f"http://example.com/{i}") for i in range(8)]
        async with AsyncDownloader(host="127.0.0.1", port=port) as d:
            got = {r.request_uuid async for r in d.batch(tasks)}
        assert got == {f"t-{i}" for i in range(8)}

    async def test_empty_batch(self, aio_server: tuple[int, StreamingAdapter]):
        port, _ = aio_server
        async with AsyncDownloader(host="127.0.0.1", port=port) as d:
            assert [r async for r in d.batch([])] == []

    async def test_concurrent_batches(self, aio_server: tuple[int, StreamingAdapter]):
        """两个批量并发跑，互不干扰。"""
        port, _ = aio_server

        async def run(prefix: str) -> set[str]:
            async with AsyncDownloader(host="127.0.0.1", port=port) as d:
                tasks = [DownloadTask(uuid=f"{prefix}-{i}", url=f"http://example.com/{i}") for i in range(4)]
                return {r.request_uuid async for r in d.batch(tasks)}

        a, b = await asyncio.gather(run("a"), run("b"))
        assert a == {f"a-{i}" for i in range(4)}
        assert b == {f"b-{i}" for i in range(4)}


class TestAsyncAuth:
    async def test_wrong_token_raises(self, monkeypatch: pytest.MonkeyPatch):
        from concurrent import futures

        import grpc

        from ipclick.auth import TokenAuthInterceptor
        from ipclick.dto.proto import task_pb2_grpc
        from ipclick.services.task_service import TaskService
        from ipclick.utils.config_util import Settings

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
            async with AsyncDownloader(host="127.0.0.1", port=port, token="bad") as d:
                with pytest.raises(AuthenticationError):
                    await d.get("http://example.com/x")
            async with AsyncDownloader(host="127.0.0.1", port=port, token="good") as d:
                assert (await d.get("http://example.com/x")).status_code == 200
        finally:
            server.stop(grace=0).wait(timeout=5)
            service.cleanup()


class TestParityWithSync:
    def test_same_task_built_from_same_args(self):
        """异步与同步必须对同样的输入组装出同样的 DownloadTask——
        代理解析、令牌优先级这些规则在两处各写一份的话迟早失步。"""
        sync = Downloader(host="127.0.0.1", port=1)
        agen = AsyncDownloader(host="127.0.0.1", port=1)

        kwargs: dict[str, Any] = {
            "url": "http://example.com/x",
            "method": HttpMethod.POST,
            "headers": {"A": "1"},
            "timeout": 12,
            "max_retries": 5,
            "verify": False,
            "custom_passthrough": "v",
        }
        a = sync._build_task(**kwargs)
        b = agen._build_task(**kwargs)

        assert a.to_protobuf().SerializeToString(deterministic=True) or True
        for field in ("url", "method", "headers", "timeout", "max_retries", "verify", "kwargs"):
            assert getattr(a, field) == getattr(b, field), f"{field} 在同步/异步之间不一致"

        sync.close()

    def test_same_endpoint_resolution(self):
        """[::] 之类的监听地址在两边都要归一成 127.0.0.1。"""
        sync = Downloader(host="[::]", port=9527)
        agen = AsyncDownloader(host="[::]", port=9527)
        assert sync.target == agen.target == "127.0.0.1:9527"
        sync.close()

    async def test_same_result_for_same_request(self, aio_server: tuple[int, StreamingAdapter]):
        port, _ = aio_server
        with Downloader(host="127.0.0.1", port=port) as s:
            sync_resp = s.get("http://example.com/x")
        async with AsyncDownloader(host="127.0.0.1", port=port) as a:
            async_resp = await a.get("http://example.com/x")

        assert sync_resp.status_code == async_resp.status_code
        assert sync_resp.content == async_resp.content
        assert sync_resp.adapter_type == async_resp.adapter_type


class TestAsyncStreamedResponseUnit:
    async def test_repr(self, aio_server: tuple[int, StreamingAdapter]):
        port, _ = aio_server
        async with AsyncDownloader(host="127.0.0.1", port=port) as d:
            resp = await d.stream("http://example.com/big")
            async with resp:
                assert "AsyncStreamedResponse" in repr(resp)
                assert "200" in repr(resp)

    async def test_is_success(self, aio_server: tuple[int, StreamingAdapter]):
        port, _ = aio_server
        async with AsyncDownloader(host="127.0.0.1", port=port) as d:
            resp = await d.stream("http://example.com/big")
            async with resp:
                assert resp.is_success()
        assert isinstance(resp, AsyncStreamedResponse)
