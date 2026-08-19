"""基于 ``grpc.aio`` 的异步客户端与流式响应封装。"""

import asyncio
from collections.abc import AsyncIterator, Iterable
from typing import Any

import grpc
from grpc import aio
from typing_extensions import override

from ipclick.dto.models import DownloadResponse, DownloadTask, HttpMethod, IPClickAdapter, ProxyConfig
from ipclick.dto.proto import task_pb2_grpc
from ipclick.exceptions import ClientClosedError, TransportError
from ipclick.rpc import open_async_channel
from ipclick.sdk import ClientBase
from ipclick.tls import TLSSettings
from ipclick.utils.log_util import log


_aio_dynamic: Any = aio
_EOF: Any = _aio_dynamic.EOF


class AsyncStreamedResponse:
    """异步流式响应；首部在工厂方法中读取，正文按需消费。"""

    def __init__(self, call: Any, header: Any):
        """用已验证的协议首部初始化响应元数据。"""
        self._call: Any = call
        self._closed: bool = False
        self._exhausted: bool = False
        self._completed: bool = False

        self.elapsed_ms: int = 0
        self.total_bytes: int = 0
        self.trailer_error: str | None = None

        self.request_uuid: str = header.request_uuid
        self.url: str = header.effective_url
        self.status_code: int = header.status_code
        self.headers: dict[str, str] = dict(header.response_headers)
        self.error: str | None = header.error_message or None
        self.content_length: int = header.content_length

    @classmethod
    async def create(cls, call: Any) -> "AsyncStreamedResponse":
        """读取并验证流首部；失败时取消无法交还给调用方的 RPC。"""
        try:
            first = await call.read()
        except asyncio.CancelledError:
            # 工厂尚未返回响应对象，取消任务时只能在这里释放 RPC。
            call.cancel()
            raise
        except grpc.RpcError as e:
            call.cancel()
            raise TransportError(f"流式下载失败: {e}") from e

        if first == _EOF:
            call.cancel()
            raise TransportError("服务端未返回任何数据")
        if not first.HasField("header"):
            call.cancel()
            raise TransportError("协议错误：流的第一条消息不是 header")
        return cls(call, first.header)

    def is_success(self) -> bool:
        """返回 HTTP 状态和服务端错误字段是否共同表示成功。"""
        return 200 <= self.status_code < 300 and not self.error and not self.trailer_error

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """异步产出正文分片，并读取 trailer 中的统计信息。"""
        if self._closed or self._exhausted:
            return
        try:
            while True:
                message = await self._call.read()
                if message == _EOF:
                    raise TransportError("协议错误：流在 trailer 到达前结束")
                if message.HasField("chunk"):
                    yield message.chunk
                elif message.HasField("trailer"):
                    self.elapsed_ms = message.trailer.response_time_ms
                    self.total_bytes = message.trailer.total_bytes
                    self.trailer_error = message.trailer.error_message or None
                    self._completed = True
                    return
                else:
                    raise TransportError("协议错误：正文中出现了重复的 header")
        except grpc.RpcError as e:
            raise TransportError(f"流式下载中断: {e}") from e
        finally:
            self._exhausted = True
            # 异步生成器被 aclose、任务取消或异常终止时，不让底层 RPC 继续占用资源。
            if not self._completed:
                self._call.cancel()

    async def read(self) -> bytes:
        """消费并拼接剩余的全部响应体。"""
        chunks = [chunk async for chunk in self]
        return b"".join(chunks)

    def close(self) -> None:
        """取消尚未消费完的 RPC；可重复调用。"""
        if self._closed:
            return
        self._closed = True
        if not self._completed:
            self._call.cancel()

    async def aclose(self) -> None:
        """取消尚未完成的 RPC，供异步资源清理代码统一调用。"""
        self.close()

    async def __aenter__(self) -> "AsyncStreamedResponse":
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        await self.aclose()

    @override
    def __repr__(self) -> str:
        return f"<AsyncStreamedResponse [{self.status_code}] {self.url}>"


class AsyncDownloader(ClientBase):
    """惰性建连的异步 IPClick gRPC 客户端。"""

    def __init__(
        self,
        config_path: str | None = None,
        host: str | None = None,
        port: int | None = None,
        token: str | None = None,
        tls: TLSSettings | None = None,
    ):
        """初始化客户端；channel 绑定到首次请求所在的事件循环。"""
        super().__init__(config_path, host, port, token, tls)
        self._channel: aio.Channel | None = None
        self._stub: task_pb2_grpc.TaskServiceStub | None = None

    def _get_stub(self) -> task_pb2_grpc.TaskServiceStub:
        if self._closed:
            raise ClientClosedError("AsyncDownloader 已关闭，无法继续发送请求")

        if self._stub is None:
            self._channel = open_async_channel(
                self.target,
                credentials=self._credentials,
                options=self._channel_options,
                compression=grpc.Compression.Gzip,
            )
            self._stub = task_pb2_grpc.TaskServiceStub(self._channel)
        return self._stub

    async def close(self) -> None:
        """异步关闭底层 channel，并禁止继续发送请求。"""
        self._closed: bool = True
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._stub = None

    async def __aenter__(self) -> "AsyncDownloader":
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        await self.close()

    async def request(
        self,
        *,
        url: str,
        adapter: IPClickAdapter | str | None = None,
        method: HttpMethod = HttpMethod.GET,
        proxy: ProxyConfig | str | bool | None = None,
        allowed_status_codes: list[int] | None = None,
        **kwargs: Any,
    ) -> DownloadResponse:
        """从便捷参数构造任务并执行；传输错误会转换为错误响应。"""
        task = self._build_task(
            url=url,
            adapter=adapter,
            method=method,
            proxy=proxy,
            allowed_status_codes=allowed_status_codes,
            **kwargs,
        )
        try:
            return await self.download(task)
        except TransportError as e:
            log.error(f"请求 {url} 失败：{e}")
            return DownloadResponse.from_error(str(e), url=url)

    async def download(self, task: DownloadTask) -> DownloadResponse:
        """提交完整任务，并对临时不可用的 gRPC 服务执行异步退避重试。"""
        stub = self._get_stub()
        pb_request = task.to_protobuf()

        for attempt in range(self.rpc_max_retries + 1):
            try:
                pb_response = await stub.Send(
                    pb_request,
                    timeout=self._deadline(task),
                    metadata=self._metadata or None,
                    compression=self.compression.for_request(pb_request),
                )
                return DownloadResponse.from_protobuf(pb_response)
            except grpc.RpcError as e:
                if not self._should_retry_rpc(e, attempt):
                    raise self._rpc_error(e) from e
                delay = self.rpc_retry_backoff * (2**attempt)
                details = e.details() if hasattr(e, "details") else str(e)
                log.warning(
                    f"连接服务端 {self.target} 失败（第 {attempt + 1}/{self.rpc_max_retries + 1} 次），"
                    f"{delay:.1f} 秒后重试：{details}"
                )
                # 异步客户端不能复用同步基类的 time.sleep，否则会阻塞事件循环。
                await asyncio.sleep(delay)

        raise TransportError(f"连接 {self.target} 失败：重试 {self.rpc_max_retries} 次后仍不可用")

    async def stream(self, url: str, **kwargs: Any) -> AsyncStreamedResponse:
        """发起服务端流式下载并读取协议首部。"""
        task = self._build_task(url=url, **kwargs)
        stub = self._get_stub()
        pb_request = task.to_protobuf()
        try:
            call = stub.SendStream(
                pb_request,
                timeout=self._deadline(task),
                metadata=self._metadata or None,
                compression=self.compression.for_request(pb_request),
            )
            return await AsyncStreamedResponse.create(call)
        except grpc.RpcError as e:
            raise self._rpc_error(e) from e

    async def batch(
        self,
        tasks: Iterable[DownloadTask],
        timeout: float | None = None,
    ) -> AsyncIterator[DownloadResponse]:
        """以双向流提交多个任务，并异步产出响应。"""
        stub = self._get_stub()

        async def _requests() -> AsyncIterator[Any]:
            for task in tasks:
                yield task.to_protobuf()

        try:
            call = stub.SendBatch(
                _requests(),
                timeout=timeout,
                metadata=self._metadata or None,
                compression=self.compression.for_stream(),
            )
            async for pb_response in call:
                yield DownloadResponse.from_protobuf(pb_response)
        except grpc.RpcError as e:
            raise self._rpc_error(e) from e

    async def get(self, url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> DownloadResponse:
        """发送 GET 请求。"""
        return await self.request(method=HttpMethod.GET, url=url, params=params, **kwargs)

    async def post(
        self, url: str, data: Any = None, json: dict[str, Any] | None = None, **kwargs: Any
    ) -> DownloadResponse:
        """发送 POST 请求。"""
        return await self.request(method=HttpMethod.POST, url=url, data=data, json=json, **kwargs)

    async def put(self, url: str, data: Any = None, **kwargs: Any) -> DownloadResponse:
        """发送 PUT 请求。"""
        return await self.request(method=HttpMethod.PUT, url=url, data=data, **kwargs)

    async def patch(self, url: str, data: Any = None, **kwargs: Any) -> DownloadResponse:
        """发送 PATCH 请求。"""
        return await self.request(method=HttpMethod.PATCH, url=url, data=data, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> DownloadResponse:
        """发送 DELETE 请求。"""
        return await self.request(method=HttpMethod.DELETE, url=url, **kwargs)

    async def head(self, url: str, **kwargs: Any) -> DownloadResponse:
        """发送 HEAD 请求。"""
        return await self.request(method=HttpMethod.HEAD, url=url, **kwargs)

    async def options(self, url: str, **kwargs: Any) -> DownloadResponse:
        """发送 OPTIONS 请求。"""
        return await self.request(method=HttpMethod.OPTIONS, url=url, **kwargs)


__all__ = ["AsyncDownloader", "AsyncStreamedResponse"]
