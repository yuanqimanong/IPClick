"""异步服务端的令牌鉴权（0.7.0）。

这组测试是补上来的——它本该在写 async_server 时就存在。

漏掉它的代价：拒绝路径原本写成了 ``aio.unary_unary_rpc_method_handler``，
而 ``grpc.aio`` **根本没有这个符号**。非法调用不会拿到 UNAUTHENTICATED，
而是在拦截器里抛 AttributeError 变成一个含糊的内部错误。功能测试全绿、
TLS 测试全绿（它们用的是**空令牌**即不启用鉴权），只有真的"带错令牌打一次"
才会暴露。最后是类型检查器先发现的。
"""

import asyncio
import contextlib
import threading
import time
from typing import Any

import grpc
import pytest

from ipclick.auth import TokenAuthInterceptor
from ipclick.dto.response import Response
from ipclick.services.async_task_service import AsyncTaskService
from ipclick.utils.config_util import Settings
from tests.test_sdk_e2e import _free_port


TOKEN = "correct-horse-battery-staple"


class _EchoAdapter:
    adapter_name = "curl_cffi"
    supports_async = True

    async def adownload(self, url: str, **kwargs: Any) -> Response:
        await asyncio.sleep(0)
        return Response(url=url, status_code=200, content=b"ok")


def _serve(tokens: tuple[str, ...], monkeypatch: pytest.MonkeyPatch):
    """在后台线程里跑一个带鉴权的 aio 服务端。"""
    from ipclick.async_server import serve_async

    adapter = _EchoAdapter()
    monkeypatch.setattr("ipclick.services.task_service.get_default_adapter", lambda settings=None: adapter)
    monkeypatch.setattr(
        "ipclick.services.task_service.get_adapter",
        lambda name, settings=None, browser_settings=None: adapter,
    )

    port = _free_port()
    ready = threading.Event()
    box: dict[str, Any] = {}

    def run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        box["loop"] = loop
        service = AsyncTaskService(Settings({}))

        async def main() -> None:
            ready.set()
            await serve_async(
                service,
                f"127.0.0.1:{port}",
                health_enabled=False,
                max_workers=4,
                max_concurrent_rpcs=64,
                max_concurrent_streams=64,
                compression=grpc.Compression.NoCompression,
                auth=TokenAuthInterceptor(tokens),
                reuseport=False,
            )

        try:
            loop.run_until_complete(main())
        except Exception:
            pass

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    ready.wait(timeout=10)
    time.sleep(0.4)
    try:
        yield port
    finally:
        loop = box.get("loop")
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)


class TestAsyncAuth:
    def test_correct_token_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ipclick import Downloader

        for port in _serve((TOKEN,), monkeypatch):
            with Downloader(host="127.0.0.1", port=port, token=TOKEN) as d:
                resp = d.get("http://example.com/x", timeout=5, max_retries=0)
            assert resp.status_code == 200

    def test_missing_token_is_rejected_with_unauthenticated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """必须是 UNAUTHENTICATED，而不是某个含糊的内部错误。

        错误码是调用方唯一的排障线索：UNAUTHENTICATED 指向'去配令牌'，
        而 UNKNOWN / INTERNAL 会让人去查网络、查服务端日志、查防火墙。
        """
        from ipclick import Downloader
        from ipclick.exceptions import AuthenticationError

        for port in _serve((TOKEN,), monkeypatch):
            with Downloader(host="127.0.0.1", port=port) as d, pytest.raises(AuthenticationError):
                d.get("http://example.com/x", timeout=5, max_retries=0)

    def test_wrong_token_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ipclick import Downloader
        from ipclick.exceptions import AuthenticationError

        for port in _serve((TOKEN,), monkeypatch):
            with Downloader(host="127.0.0.1", port=port, token="wrong") as d, pytest.raises(AuthenticationError):
                d.get("http://example.com/x", timeout=5, max_retries=0)

    def test_no_tokens_configured_means_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """没配令牌 = 不鉴权，与同步服务端行为一致（升级不会突然全部中断）。"""
        from ipclick import Downloader

        for port in _serve((), monkeypatch):
            with Downloader(host="127.0.0.1", port=port) as d:
                resp = d.get("http://example.com/x", timeout=5, max_retries=0)
            assert resp.status_code == 200


class TestDenyHandlerExists:
    def test_grpc_has_the_handler_factory_we_use(self) -> None:
        """回归：拒绝用的工厂函数在 grpc 而不是 grpc.aio 上。

        写成 aio.unary_unary_rpc_method_handler 会在**拒绝的那一刻**才抛
        AttributeError——正常路径全绿，只有非法调用打进来才炸。
        """
        from grpc import aio

        assert hasattr(grpc, "unary_unary_rpc_method_handler")
        assert not hasattr(aio, "unary_unary_rpc_method_handler"), (
            "grpc.aio 现在有这个符号了，可以简化 async_server 里的注释"
        )
