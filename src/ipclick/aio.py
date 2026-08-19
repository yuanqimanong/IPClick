from collections.abc import AsyncIterator, Iterable
from typing import Any

import grpc
from grpc import aio
from typing_extensions import override

from ipclick.dto.models import DownloadResponse, DownloadTask, HttpMethod, IPClickAdapter, ProxyConfig
from ipclick.dto.proto import task_pb2_grpc
from ipclick.exceptions import ClientClosedError, TransportError
from ipclick.sdk import ClientBase
from ipclick.tls import TLSSettings
from ipclick.utils.log_util import log


_aio_dynamic: Any = aio
_EOF: Any = _aio_dynamic.EOF


class AsyncStreamedResponse:
    def __init__(self, call: Any, header: Any):
        self._call: Any = call
        self._closed: bool = False
        self._exhausted: bool = False

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
        chunks = [chunk async for chunk in self]
        return b"".join(chunks)

    def close(self) -> None:
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
    def __init__(
        self,
        config_path: str | None = None,
        host: str | None = None,
        port: int | None = None,
        token: str | None = None,
        tls: TLSSettings | None = None,
    ):
        super().__init__(config_path, host, port, token, tls)
        self._channel: aio.Channel | None = None
        self._stub: task_pb2_grpc.TaskServiceStub | None = None

    def _get_stub(self) -> task_pb2_grpc.TaskServiceStub:
        if self._closed:
            raise ClientClosedError("AsyncDownloader 已关闭，无法继续发送请求")

        if self._stub is None:
            self._channel = (
                aio.secure_channel(
                    self.target,
                    self._credentials,
                    options=self._channel_options,
                    compression=grpc.Compression.Gzip,
                )
                if self._credentials is not None
                else aio.insecure_channel(
                    self.target,
                    options=self._channel_options,
                    compression=grpc.Compression.Gzip,
                )
            )
            self._stub = task_pb2_grpc.TaskServiceStub(self._channel)
        return self._stub

    async def close(self) -> None:
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
        stub = self._get_stub()
        pb_request = task.to_protobuf()
        try:
            pb_response = await stub.Send(
                pb_request,
                timeout=self._deadline(task),
                metadata=self._metadata or None,
                compression=self.compression.for_request(pb_request),
            )
            return DownloadResponse.from_protobuf(pb_response)
        except grpc.RpcError as e:
            raise self._rpc_error(e) from e

    async def stream(self, url: str, **kwargs: Any) -> AsyncStreamedResponse:
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
