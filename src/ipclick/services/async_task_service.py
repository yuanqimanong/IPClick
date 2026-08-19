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
from ipclick.services.task_service import CallerGone, TaskService, caller_still_waiting, is_forwarded
from ipclick.trace import RequestTrace
from ipclick.utils.log_util import log
from ipclick.utils.url_util import validate_url


if TYPE_CHECKING:
    from collections.abc import Iterator


class AsyncTaskService(TaskService):
    _loop: "asyncio.AbstractEventLoop | None" = None

    def bind_loop(self, loop: "asyncio.AbstractEventLoop") -> None:
        self._loop = loop

    def send_from_thread(self, request: "task_pb2.ReqTask", context: Any, timeout: float = 300.0) -> Any:
        loop = self._loop
        if loop is None or loop.is_closed():
            raise RuntimeError("异步服务端尚未就绪（事件循环未绑定），请稍后再试")
        future = asyncio.run_coroutine_threadsafe(self.Send(request, context), loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            raise

    @override
    def limiters_for_sharding(self) -> list[Any]:
        return [self._async_limiter]

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
                if not caller_still_waiting(context):
                    log.debug("Request {} 在开工前发现调用方已断开，放弃", request.uuid)
                    raise CallerGone

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
        waiting_since = time.monotonic()
        async with self._async_limiter.acquire(request.url):
            if tr is not None:
                tr.queued_ms = int((time.monotonic() - waiting_since) * 1000)
            return await adapter.adownload(request.url, **self._build_download_kwargs(request))

    @property
    def _async_limiter(self) -> "AsyncHostLimiter":
        limiter = self.__dict__.get("_async_limiter_cache")
        if limiter is None:
            limiter = build_async_limiter(dict(self.config.get("DOWNLOADER", {})))
            self.__dict__["_async_limiter_cache"] = limiter
        return limiter

    @override
    async def SendStream(
        self, request: "task_pb2.ReqTask", context: ServicerContext
    ) -> AsyncIterator["task_pb2.TaskRespChunk"]:
        loop = asyncio.get_running_loop()
        iterator: Iterator[task_pb2.TaskRespChunk] = super().SendStream(request, context)

        def _next() -> "task_pb2.TaskRespChunk | None":
            return next(iterator, None)

        while True:
            chunk = await loop.run_in_executor(None, _next)
            if chunk is None:
                return
            yield chunk

    @override
    async def SendBatch(self, request_iterator: Any, context: ServicerContext) -> AsyncIterator["task_pb2.TaskResp"]:
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
