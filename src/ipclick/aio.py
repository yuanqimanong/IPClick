"""异步客户端（基于 ``grpc.aio``）。

与同步的 :class:`~ipclick.sdk.Downloader` 并存，接口一一对应，配置、令牌解析和
任务组装都复用 :class:`~ipclick.sdk.ClientBase`——两边对同样的输入必须产生
同样的 DownloadTask。

::

    from ipclick.aio import AsyncDownloader

    async with AsyncDownloader() as d:
        resp = await d.get("https://example.com")

        async with await d.stream("https://example.com/big.zip") as s:
            async for chunk in s:
                ...

        async for resp in d.batch(tasks):
            ...
"""

from collections.abc import AsyncIterator, Iterable
from typing import Any

import grpc
from grpc import aio
from typing_extensions import override

from ipclick.dto.models import DownloadResponse, DownloadTask, HttpMethod, IPClickAdapter, ProxyConfig
from ipclick.dto.proto import task_pb2_grpc
from ipclick.exceptions import ClientClosedError, TransportError
from ipclick.sdk import CHANNEL_OPTIONS, ClientBase
from ipclick.utils.log_util import log


# grpc.aio 没有随包发 .pyi，EOF 这个哨兵在类型检查器眼里不存在，运行时是有的。
# 抽成模块级常量，忽略只写一处而不是每个使用点都写。
_EOF: Any = aio.EOF  # pyright: ignore[reportAttributeAccessIssue]


class AsyncStreamedResponse:
    """流式下载的异步响应句柄。

    与同步版一样，构造后 ``status_code`` / ``headers`` 立刻可用，body 按需迭代。
    但异步版的 header 必须在 ``await`` 里读，所以用 :meth:`create` 而不是
    直接构造。
    """

    def __init__(self, call: Any, header: Any):
        self._call: Any = call
        self._closed: bool = False
        self._exhausted: bool = False

        #: 以下三项要等 trailer 到达（即 body 读完）之后才有值
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
        """读出第一条 header 消息后构造实例。"""
        try:
            first = await call.read()
        except grpc.RpcError as e:
            raise TransportError(f"流式下载失败: {e}") from e

        if first == _EOF:
            raise TransportError("服务端未返回任何数据")
        if not first.HasField("header"):
            raise TransportError("协议错误：流的第一条消息不是 header")
        return cls(call, first.header)

    def is_success(self) -> bool:
        return 200 <= self.status_code < 300 and not self.error

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """迭代响应体分片。trailer 到达后自动停止并记录统计信息。"""
        if self._exhausted:
            return
        try:
            while True:
                message = await self._call.read()
                if message == _EOF:
                    break
                if message.HasField("chunk"):
                    yield message.chunk
                elif message.HasField("trailer"):
                    self.elapsed_ms = message.trailer.response_time_ms
                    self.total_bytes = message.trailer.total_bytes
                    self.trailer_error = message.trailer.error_message or None
                    break
        except grpc.RpcError as e:
            raise TransportError(f"流式下载中断: {e}") from e
        finally:
            self._exhausted = True

    async def read(self) -> bytes:
        """把剩余分片全部读进内存。大文件请直接迭代。"""
        chunks = [chunk async for chunk in self]
        return b"".join(chunks)

    def close(self) -> None:
        """取消这条流。已经读完的话是 no-op。"""
        if self._closed:
            return
        self._closed = True
        if not self._exhausted:
            self._call.cancel()

    async def __aenter__(self) -> "AsyncStreamedResponse":
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()

    @override
    def __repr__(self) -> str:
        return f"<AsyncStreamedResponse [{self.status_code}] {self.url}>"


class AsyncDownloader(ClientBase):
    """IPClick 异步下载器客户端。

    与同步版的差异只在于调用方式；参数含义、默认值、异常类型完全一致。
    """

    def __init__(
        self,
        config_path: str | None = None,
        host: str | None = None,
        port: int | None = None,
        token: str | None = None,
    ):
        super().__init__(config_path, host, port, token)
        self._channel: aio.Channel | None = None
        self._stub: task_pb2_grpc.TaskServiceStub | None = None

    # ------------------------------------------------------------------ #
    # 连接管理
    # ------------------------------------------------------------------ #

    def _get_stub(self) -> task_pb2_grpc.TaskServiceStub:
        """惰性创建并复用 channel。

        aio.Channel 必须在事件循环里创建，所以不能放在 __init__ 里——
        构造 AsyncDownloader 时可能还没有运行中的循环。
        """
        if self._closed:
            raise ClientClosedError("AsyncDownloader 已关闭，无法继续发送请求")

        if self._stub is None:
            self._channel = aio.insecure_channel(
                self.target,
                options=CHANNEL_OPTIONS,
                compression=grpc.Compression.Gzip,
            )
            self._stub = task_pb2_grpc.TaskServiceStub(self._channel)
        return self._stub

    async def close(self) -> None:
        """关闭底层 gRPC channel。可重复调用。"""
        self._closed: bool = True
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._stub = None

    async def __aenter__(self) -> "AsyncDownloader":
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        await self.close()

    # ------------------------------------------------------------------ #
    # 请求
    # ------------------------------------------------------------------ #

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
        """发送一次下载请求。

        与同步版一致：不抛网络异常，失败时返回 ``status_code == -1`` 的响应；
        参数非法仍会抛 ValidationError。
        """
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
        """执行下载任务。

        Raises:
            TransportError: 与服务端通信失败。
            AuthenticationError: 鉴权失败。
        """
        stub = self._get_stub()
        try:
            pb_response = await stub.Send(
                task.to_protobuf(),
                timeout=self._deadline(task),
                metadata=self._metadata or None,
            )
            return DownloadResponse.from_protobuf(pb_response)
        except grpc.RpcError as e:
            raise self._rpc_error(e) from e

    # ------------------------------------------------------------------ #
    # 流式下载
    # ------------------------------------------------------------------ #

    async def stream(self, url: str, **kwargs: Any) -> AsyncStreamedResponse:
        """流式下载，返回可异步迭代的响应句柄。"""
        task = self._build_task(url=url, **kwargs)
        stub = self._get_stub()
        try:
            call = stub.SendStream(
                task.to_protobuf(),
                timeout=self._deadline(task),
                metadata=self._metadata or None,
            )
            return await AsyncStreamedResponse.create(call)
        except grpc.RpcError as e:
            raise self._rpc_error(e) from e

    # ------------------------------------------------------------------ #
    # 批量
    # ------------------------------------------------------------------ #

    async def batch(
        self,
        tasks: Iterable[DownloadTask],
        timeout: float | None = None,
    ) -> AsyncIterator[DownloadResponse]:
        """批量下载。结果按**完成顺序**产出，靠 ``request_uuid`` 对应回请求。"""
        stub = self._get_stub()

        async def _requests() -> AsyncIterator[Any]:
            for task in tasks:
                yield task.to_protobuf()

        try:
            call = stub.SendBatch(_requests(), timeout=timeout, metadata=self._metadata or None)
            async for pb_response in call:
                yield DownloadResponse.from_protobuf(pb_response)
        except grpc.RpcError as e:
            raise self._rpc_error(e) from e

    # ------------------------------------------------------------------ #
    # 便捷方法
    # ------------------------------------------------------------------ #

    async def get(self, url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> DownloadResponse:
        return await self.request(method=HttpMethod.GET, url=url, params=params, **kwargs)

    async def post(
        self, url: str, data: Any = None, json: dict[str, Any] | None = None, **kwargs: Any
    ) -> DownloadResponse:
        return await self.request(method=HttpMethod.POST, url=url, data=data, json=json, **kwargs)

    async def put(self, url: str, data: Any = None, **kwargs: Any) -> DownloadResponse:
        return await self.request(method=HttpMethod.PUT, url=url, data=data, **kwargs)

    async def patch(self, url: str, data: Any = None, **kwargs: Any) -> DownloadResponse:
        return await self.request(method=HttpMethod.PATCH, url=url, data=data, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> DownloadResponse:
        return await self.request(method=HttpMethod.DELETE, url=url, **kwargs)

    async def head(self, url: str, **kwargs: Any) -> DownloadResponse:
        return await self.request(method=HttpMethod.HEAD, url=url, **kwargs)

    async def options(self, url: str, **kwargs: Any) -> DownloadResponse:
        return await self.request(method=HttpMethod.OPTIONS, url=url, **kwargs)


__all__ = ["AsyncDownloader", "AsyncStreamedResponse"]
