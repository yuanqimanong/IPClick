"""适配器的可选异步接口（0.7.0）。

核心约束是**加法不破坏**：项目声明支持注册自定义适配器，它们只实现了同步
``download()``。异步化必须让它们一行不改照常工作，否则就是一次比换并发模型
更狠的破坏性变更——而且报错信息（``object Response can't be used in 'await'
expression``）和真因（"你的适配器该改成 async 了"）之间毫无字面联系。
"""

from typing import Any

import pytest

from ipclick.adapters.base import DownloaderAdapter
from ipclick.dto.response import Response


class SyncOnlyAdapter(DownloaderAdapter):
    """只实现同步 download 的适配器——代表所有第三方实现。"""

    adapter_name = "sync_only"

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def download(self, url: str, **kwargs: Any) -> Response:  # type: ignore[override]
        self.calls.append(url)
        return Response(url=url, status_code=200, content=b"sync")


class TestFallbackKeepsSyncAdaptersWorking:
    def test_default_is_not_async(self) -> None:
        assert DownloaderAdapter.supports_async is False
        assert SyncOnlyAdapter().supports_async is False

    async def test_adownload_falls_back_to_the_sync_implementation(self) -> None:
        """没实现异步的适配器，await 它照样拿到正确结果。"""
        adapter = SyncOnlyAdapter()
        resp = await adapter.adownload("http://example.invalid/x", method="GET")
        assert resp.status_code == 200
        assert resp.content == b"sync"
        assert adapter.calls == ["http://example.invalid/x"]

    async def test_fallback_does_not_block_the_event_loop(self) -> None:
        """回退走线程池，所以多个请求应当并行而不是排队。

        串行的话 5 个 × 0.1s = 0.5s；并行应当接近 0.1s。
        这一条防的是"回退实现写成了直接调用"——那样功能正确但把
        异步模式的并发性悄悄退化成串行，测不出来只会表现为吞吐上不去。
        """
        import asyncio
        import time

        class SlowSync(SyncOnlyAdapter):
            def download(self, url: str, **kwargs: Any) -> Response:  # type: ignore[override]
                time.sleep(0.1)
                return Response(url=url, status_code=200, content=b"slow")

        adapter = SlowSync()
        started = time.perf_counter()
        await asyncio.gather(*(adapter.adownload(f"http://x/{i}", method="GET") for i in range(5)))
        assert time.perf_counter() - started < 0.35, "回退实现没有真正并行"

    async def test_adownload_stream_falls_back_lazily(self) -> None:
        """流式回退要逐个搬分片，不能先 list() 再吐。

        先收完再吐等于把流式的意义（响应体不整个进内存）丢掉，
        而接口行为看起来完全一样。
        """
        produced: list[int] = []

        class StreamingSync(SyncOnlyAdapter):
            def download_stream(self, url: str, **kwargs: Any) -> Any:  # type: ignore[override]
                from ipclick.adapters.base import StreamHeader

                yield StreamHeader(url=url, status_code=200)
                for i in range(3):
                    produced.append(i)
                    yield f"chunk{i}".encode()

        adapter = StreamingSync()
        seen = 0
        async for _ in adapter.adownload_stream("http://x/s"):
            # 第一个分片被消费时，后面的还不该被生产出来
            if seen == 1:
                assert len(produced) <= 2, f"分片被提前收完了：{produced}"
            seen += 1
        assert seen == 4  # 1 个 header + 3 个分片


@pytest.mark.network
class TestRealAsyncImplementations:
    """curl_cffi / niquests 声明了 supports_async，走的是各自的 AsyncSession。"""

    def test_curl_cffi_declares_async(self) -> None:
        from ipclick.adapters.curl_cffi_adapter import CurlCffiAdapter

        assert CurlCffiAdapter.supports_async is True

    def test_niquests_declares_async(self) -> None:
        from ipclick.adapters.niquests_adapter import NiquestsAdapter

        assert NiquestsAdapter.supports_async is True


class TestAsyncRetryDoesNotBlockTheLoop:
    async def test_backoff_uses_asyncio_sleep(self) -> None:
        """异步重试的退避必须让出事件循环。

        用 time.sleep 的话不是拖慢这一个请求，而是让同一个循环上**所有**
        在飞的请求一起停住——默认退避 1+2+4 秒，一次重试就冻结 worker 七秒，
        现象却是"毫不相干的请求也集体变慢"，极难联想到病根。
        """
        import asyncio

        from ipclick.adapters.base import aretry

        class Flaky(DownloaderAdapter):
            adapter_name = "flaky"

            def download(self, url: str, **kwargs: Any) -> Response:  # pragma: no cover
                raise NotImplementedError

            @aretry()
            async def adownload(self, url: str, **kwargs: Any) -> Response:
                raise RuntimeError("总是失败")

        adapter = Flaky()
        progressed = 0

        async def other_work() -> None:
            nonlocal progressed
            for _ in range(20):
                await asyncio.sleep(0.005)
                progressed += 1

        await asyncio.gather(
            adapter.adownload("http://x", max_retries=1, retry_delay=0.05),
            other_work(),
        )
        assert progressed == 20, "退避期间事件循环被阻塞了，其他协程没能推进"
