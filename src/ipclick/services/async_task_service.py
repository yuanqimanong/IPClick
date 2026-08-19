"""``grpc.aio`` 下的任务服务（0.7.0，实验性）。

和同步的 :class:`~ipclick.services.task_service.TaskService` **并存**，由
``[SERVER].async_mode`` 选用哪一个。不是替换——同步那条路仍然是默认。

为什么值得做：服务端原本是一请求一线程，实测 16 核机器上单进程只能用出
1.5 个核，而线程切换与 GIL convoy 就占掉约 12% 的 GIL 时间。协程去掉的正是
这一层：适配器层单独实测 curl_cffi 从 524 QPS（50 线程）涨到 1361 QPS。

**协程解决不了的**：单进程仍然只能用一个核。要吃满多核还得靠
``[SERVER].processes``（0.6.0 加的多进程分片）。两者叠加才是终态。

继承 TaskService 而不是另起一份：适配器缓存、URL 准入、链路记录、响应组装、
异常到 gRPC 状态码的映射全部复用。**只覆写四个 RPC 入口**，其余一行不改——
两份拷贝迟早失步，而失步的表现是"同一个错误在异步模式下把人指向了另一个
排障方向"，比少一个分支更糟。
"""

import asyncio
from collections.abc import AsyncIterator
import time
from typing import TYPE_CHECKING, Any

from grpc import ServicerContext
from typing_extensions import override

from ipclick.adapters.base import DownloaderAdapter
from ipclick.async_limiter import AsyncHostLimiter, build_async_limiter
from ipclick.dto.models import METHOD_MAP, IPClickAdapter
from ipclick.dto.proto import task_pb2
from ipclick.dto.response import Response
from ipclick.services.task_service import TaskService, _caller_still_waiting, _CallerGone, is_forwarded
from ipclick.trace import RequestTrace
from ipclick.utils.log_util import log
from ipclick.utils.url_util import validate_url


if TYPE_CHECKING:
    from collections.abc import Iterator


class AsyncTaskService(TaskService):
    """异步版任务服务。只覆写 RPC 入口，其余复用父类。"""

    @override
    async def Send(self, request: "task_pb2.ReqTask", context: ServicerContext) -> "task_pb2.TaskResp":
        log.debug("Received request: {} for URL: {}", request.uuid, request.url)
        start_time = time.monotonic()
        method_name = METHOD_MAP.get(request.method, "GET")

        try:
            adapter_name = IPClickAdapter.from_pb(request.adapter).display_name
        except ValueError:
            adapter_name = "unknown"

        with self._recorder.track_request(adapter_name, method_name, uuid=request.uuid, url=request.url) as tr:
            tr.node_id = self.node_id
            tr.forwarded = is_forwarded(context)
            try:
                adapter_member = IPClickAdapter.from_pb(request.adapter)
                adapter = self._get_cached_adapter(adapter_member.display_name)
                tr.adapter = adapter.adapter_name

                validate_url(request.url, self.url_policy)
                if not _caller_still_waiting(context):
                    log.debug("Request {} 在开工前发现调用方已断开，放弃", request.uuid)
                    raise _CallerGone

                response = await self._aexecute_download(adapter, request, tr)
                tr.attempts = response.attempts
                grpc_response = self._build_grpc_response(request, response, tr)
            except Exception as e:
                grpc_response = self._response_for_exception(e, request, tr, context)

            tr.status_code = grpc_response.status_code
            tr.size = len(grpc_response.content)
            tr.error = grpc_response.error_message

        grpc_response.response_time_ms = int((time.monotonic() - start_time) * 1000)
        log.debug(
            "Request {} completed in {}ms, status: {}, adapter: {}",
            request.uuid,
            grpc_response.response_time_ms,
            grpc_response.status_code,
            adapter_name,
        )
        return grpc_response

    async def _aexecute_download(
        self, adapter: DownloaderAdapter, request: "task_pb2.ReqTask", tr: RequestTrace | None = None
    ) -> Response:
        """执行一次下载。按适配器是否支持异步分派。

        ``supports_async`` 为假时走基类的回退（同步实现丢进线程池）——那样拿不到
        协程的好处，但**语义完全一致**，第三方适配器不用改一行就能在异步模式下跑。
        """
        waiting_since = time.monotonic()
        async with self._async_limiter.acquire(request.url):
            if tr is not None:
                tr.queued_ms = int((time.monotonic() - waiting_since) * 1000)
            return await adapter.adownload(request.url, **self._build_download_kwargs(request))

    @property
    def _async_limiter(self) -> "AsyncHostLimiter":
        """按 host 的异步限流闸门。惰性建：它内部的 asyncio 原语要在事件循环里造。"""
        limiter = self.__dict__.get("_async_limiter_cache")
        if limiter is None:
            limiter = build_async_limiter(dict(self.config.get("DOWNLOADER", {})))
            self.__dict__["_async_limiter_cache"] = limiter
        return limiter

    @override
    async def SendStream(
        self, request: "task_pb2.ReqTask", context: ServicerContext
    ) -> AsyncIterator["task_pb2.TaskRespChunk"]:
        """流式下载。

        目前直接复用父类的同步实现，逐个分片搬到线程池里取——响应体仍然不会
        整个进内存（那是流式的核心性质），但这条路上每个在飞的流占一个线程。
        流式请求本来就少而长，优先级低于 Send，留待后续。
        """
        loop = asyncio.get_running_loop()
        iterator: Iterator[task_pb2.TaskRespChunk] = super().SendStream(request, context)
        sentinel = object()
        while True:
            chunk = await loop.run_in_executor(None, next, iterator, sentinel)
            if chunk is sentinel:
                return
            yield chunk  # type: ignore[misc]

    @override
    async def SendBatch(self, request_iterator: Any, context: ServicerContext) -> AsyncIterator["task_pb2.TaskResp"]:
        """批量下载：结果按**完成顺序**产出，不是提交顺序。

        异步版用 ``asyncio.as_completed`` 而不是线程池——批量本来就是"量大"的
        场景，正是协程最划算的地方。并发度仍然沿用 ``[SERVER].max_workers``：
        这里不受线程数限制，但下游目标站点受得了多少并没有因此改变。
        """
        semaphore = asyncio.Semaphore(self._batch_concurrency)

        async def one(req: "task_pb2.ReqTask") -> "task_pb2.TaskResp":
            async with semaphore:
                return await self.Send(req, context)

        pending: list[asyncio.Task[task_pb2.TaskResp]] = []
        async for req in request_iterator:
            pending.append(asyncio.create_task(one(req)))

        for completed in asyncio.as_completed(pending):
            yield await completed

    @override
    async def Ping(self, request: "task_pb2.PingReq", context: ServicerContext) -> "task_pb2.PingResp":
        return super().Ping(request, context)


__all__ = ["AsyncTaskService"]
