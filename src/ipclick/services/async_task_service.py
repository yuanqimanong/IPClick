from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
import time
from typing import Any

from grpc import ServicerContext
from typing_extensions import override

from ipclick.adapters.base import DownloaderAdapter
from ipclick.async_limiter import AsyncHostLimiter, build_async_limiter
from ipclick.dto.proto import task_pb2
from ipclick.dto.response import Response
from ipclick.protocols import ShardableLimiter
from ipclick.services.task_service import TaskService
from ipclick.trace import RequestTrace
from ipclick.utils.config_util import Settings, section


DEFAULT_THREAD_HANDOFF_TIMEOUT = 300.0


class AsyncTaskService(TaskService):
    def __init__(self, config: Settings, *args: Any, **kwargs: Any) -> None:
        super().__init__(config, *args, **kwargs)
        self._loop: asyncio.AbstractEventLoop | None = None
        self.async_limiter: AsyncHostLimiter = build_async_limiter(section(self.config, "DOWNLOADER"))

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def send_from_thread(
        self,
        request: task_pb2.ReqTask,
        context: Any,
        timeout: float = DEFAULT_THREAD_HANDOFF_TIMEOUT,
    ) -> task_pb2.TaskResp:
        loop = self._loop
        if loop is None or loop.is_closed():
            raise RuntimeError("异步服务端尚未就绪（事件循环未绑定），请稍后再试")
        future = asyncio.run_coroutine_threadsafe(self.Send(request, context), loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            _ = future.cancel()
            raise

    @override
    def limiters_for_sharding(self) -> list[ShardableLimiter]:
        return [self.async_limiter]

    @override
    async def Send(self, request: task_pb2.ReqTask, context: ServicerContext) -> task_pb2.TaskResp:
        started = time.monotonic()
        with self.track(request, context) as tr:
            try:
                adapter = self.prepare(request, context, tr)
                response = await self._aexecute_download(adapter, request, tr)
                grpc_response = self.accept(request, response, tr)
            except Exception as e:
                grpc_response = self._response_for_exception(e, request, tr, context)
            self.record_outcome(tr, grpc_response)
        return self.stamp_elapsed(grpc_response, started, request)

    async def _aexecute_download(
        self, adapter: DownloaderAdapter, request: task_pb2.ReqTask, tr: RequestTrace | None = None
    ) -> Response:
        waiting_since = time.monotonic()
        async with self.async_limiter.acquire(request.url):
            if tr is not None:
                tr.queued_ms = int((time.monotonic() - waiting_since) * 1000)
            return await adapter.adownload(request.url, **self._build_download_kwargs(request))

    @override
    async def SendStream(
        self, request: task_pb2.ReqTask, context: ServicerContext
    ) -> AsyncIterator[task_pb2.TaskRespChunk]:
        loop = asyncio.get_running_loop()
        iterator: Iterator[task_pb2.TaskRespChunk] = super().SendStream(request, context)

        def _next() -> task_pb2.TaskRespChunk | None:
            return next(iterator, None)

        while True:
            chunk = await loop.run_in_executor(None, _next)
            if chunk is None:
                return
            yield chunk

    @override
    async def SendBatch(self, request_iterator: Any, context: ServicerContext) -> AsyncIterator[task_pb2.TaskResp]:
        semaphore = asyncio.Semaphore(self._batch_concurrency)

        async def one(req: task_pb2.ReqTask) -> task_pb2.TaskResp:
            async with semaphore:
                return await self.Send(req, context)

        pending = [asyncio.create_task(one(req)) async for req in request_iterator]
        for completed in asyncio.as_completed(pending):
            yield await completed

    @override
    async def Ping(self, request: task_pb2.PingReq, context: ServicerContext) -> task_pb2.PingResp:
        return super().Ping(request, context)


__all__ = ["AsyncTaskService"]
