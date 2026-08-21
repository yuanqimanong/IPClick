"""异步任务 RPC：协程限流、批处理和同步适配器桥接。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
import contextlib
import time
from typing import Any

from grpc import ServicerContext
from typing_extensions import override

from ipclick.adapters.base import DownloaderAdapter, StreamEvent
from ipclick.adapters.retry import caller_alive_check
from ipclick.async_limiter import AsyncHostLimiter, build_async_limiter
from ipclick.dto.proto import task_pb2
from ipclick.dto.response import Response
from ipclick.protocols import ShardableLimiter
from ipclick.services.detached import DetachedContext
from ipclick.services.task_service import TaskService, batch_metadata, caller_still_waiting
from ipclick.trace import RequestTrace
from ipclick.utils.config_util import Settings, section


DEFAULT_THREAD_HANDOFF_TIMEOUT = 300.0


class AsyncTaskService(TaskService):
    """使用异步限流器和适配器 ``adownload`` 的任务服务。"""

    def __init__(self, config: Settings, *args: Any, **kwargs: Any) -> None:
        super().__init__(config, *args, **kwargs)
        self._loop: asyncio.AbstractEventLoop | None = None
        self.async_limiter: AsyncHostLimiter = build_async_limiter(section(self.config, "DOWNLOADER"))

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """绑定运行服务的事件循环，供 Web 管理线程安全投递请求。"""
        self._loop = loop

    def send_from_thread(
        self,
        request: task_pb2.ReqTask,
        context: Any,
        timeout: float = DEFAULT_THREAD_HANDOFF_TIMEOUT,
    ) -> task_pb2.TaskResp:
        """从非事件循环线程提交 Send，并同步等待结果。"""
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
        """返回异步服务用于集群配额分片的限流器。"""
        return [self.async_limiter]

    @override
    async def Send(self, request: task_pb2.ReqTask, context: ServicerContext) -> task_pb2.TaskResp:
        """异步执行单个下载并复用统一异常映射与追踪。"""
        started = time.monotonic()
        with self.track(request, context) as tr:
            try:
                adapter = self.prepare(request, context, tr)
                response = await self._aexecute_download(adapter, request, tr, context)
                grpc_response = self.accept(request, response, tr)
            except Exception as e:
                grpc_response = self._response_for_exception(e, request, tr, context)
            self.record_outcome(tr, grpc_response)
        return self.stamp_elapsed(grpc_response, started, request)

    async def _aexecute_download(
        self,
        adapter: DownloaderAdapter,
        request: task_pb2.ReqTask,
        tr: RequestTrace | None = None,
        context: ServicerContext | None = None,
    ) -> Response:
        waiting_since = time.monotonic()
        async with self.async_limiter.acquire(request.url):
            if tr is not None:
                tr.queued_ms = int((time.monotonic() - waiting_since) * 1000)
            # 与同步版同理：deadline 过了就别再重投（ContextVar 在同一个 task 内传播）。
            token = caller_alive_check.set(None if context is None else lambda: caller_still_waiting(context))
            try:
                return await adapter.adownload(request.url, **self._build_download_kwargs(request))
            finally:
                caller_alive_check.reset(token)

    @override
    def _limited_stream(self, url: str, stream: Iterator[StreamEvent]) -> Iterator[StreamEvent]:
        """异步服务不在这里限流——流式配额由 SendStream 用 async_limiter 统一持有。

        继承来的实现用的是同步 ``host_limiter``，那是**另一个独立的配额池**：
        unary 走 async_limiter、流式走 host_limiter，两边各算一次，单 host 的有效
        并发直接翻倍；而且 limiters_for_sharding() 只上报 async_limiter，同步那份
        永远拿不到集群限流分片。所以这里只保证关流，配额在上层取。
        """
        _ = url
        return self._closing_stream(stream)

    @override
    async def SendStream(
        self, request: task_pb2.ReqTask, context: ServicerContext
    ) -> AsyncIterator[task_pb2.TaskRespChunk]:
        """在线程池逐项推进同步流迭代器，避免阻塞事件循环。"""
        async with self.async_limiter.acquire(request.url):
            async for chunk in self._stream_chunks(request, context):
                yield chunk

    async def _stream_chunks(
        self, request: task_pb2.ReqTask, context: ServicerContext
    ) -> AsyncIterator[task_pb2.TaskRespChunk]:
        loop = asyncio.get_running_loop()
        iterator: Iterator[task_pb2.TaskRespChunk] = super().SendStream(request, context)
        next_call: asyncio.Future[task_pb2.TaskRespChunk | None] | None = None

        def _next() -> task_pb2.TaskRespChunk | None:
            return next(iterator, None)

        try:
            while True:
                next_call = loop.run_in_executor(None, _next)
                # shield 保留底层 Future；协程取消时先等正在执行的 next() 收尾，
                # 再关闭生成器，避免并发 close 导致 "generator already executing"。
                chunk = await asyncio.shield(next_call)
                if chunk is None:
                    return
                yield chunk
        finally:
            if next_call is not None and not next_call.done():
                with contextlib.suppress(Exception):
                    await next_call
            closer = getattr(iterator, "close", None)
            if callable(closer):
                # 客户端取消协程时也要触发同步生成器 finally，释放 HTTP 流和限流槽。
                await loop.run_in_executor(None, closer)

    @override
    async def SendBatch(self, request_iterator: Any, context: ServicerContext) -> AsyncIterator[task_pb2.TaskResp]:
        """以固定并发窗口处理批任务，避免为无限输入一次性创建全部 Task。"""
        semaphore = asyncio.Semaphore(self._batch_concurrency)
        metadata = batch_metadata(context)

        async def one(req: task_pb2.ReqTask) -> task_pb2.TaskResp:
            async with semaphore:
                # 单个任务的 INVALID_ARGUMENT 等状态不能污染整个批处理 RPC。
                detached = DetachedContext(metadata).as_servicer_context()
                return await self.Send(req, detached)

        pending: set[asyncio.Task[task_pb2.TaskResp]] = set()
        try:
            async for req in request_iterator:
                pending.add(asyncio.create_task(one(req)))
                if len(pending) < self._batch_concurrency:
                    continue
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for completed in done:
                    yield completed.result()

            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for completed in done:
                    yield completed.result()
        finally:
            # 流被取消或下游停止读取时，不让已排队的下载继续占用资源。
            for task in pending:
                _ = task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    @override
    async def Ping(self, request: task_pb2.PingReq, context: ServicerContext) -> task_pb2.PingResp:
        """异步返回节点身份和运行状态。"""
        return super().Ping(request, context)


__all__ = ["AsyncTaskService"]
