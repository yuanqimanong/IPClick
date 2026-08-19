from collections.abc import Iterable, Iterator
import json as json_lib
import threading
import time
from typing import Any

import grpc
from typing_extensions import override

from ipclick.auth import AUTH_TOKEN_ENV, build_client_metadata, load_tokens
from ipclick.compression import CompressionPolicy
from ipclick.config_loader import load_config
from ipclick.dto.models import DownloadResponse, DownloadTask, HttpMethod, IPClickAdapter, ProxyConfig
from ipclick.dto.proto import task_pb2, task_pb2_grpc
from ipclick.exceptions import (
    AdapterError,
    AuthenticationError,
    ClientClosedError,
    TransportError,
    ValidationError,
)
from ipclick.limiter import HostLimitTimeout
from ipclick.ports import DEFAULT_GRPC_PORT, port_hint
from ipclick.rpc import client_options, credentials_for, open_channel
from ipclick.rpc.options import KEEPALIVE_TIME_MS, MIN_PING_INTERVAL_WITHOUT_DATA_MS
from ipclick.secrets import proxy_config
from ipclick.tls import TLSSettings, describe
from ipclick.utils.coerce import as_float, as_int
from ipclick.utils.config_util import Settings, section
from ipclick.utils.log_util import log


_RPC_TIMEOUT_MARGIN = 30.0

_REFUSED_MARKERS = ("refused_stream", "concurrent rpc limit")

_KEEPALIVE_MARKERS = ("too_many_pings", "keepalive")


def unavailable_hint(details: str | None, port: int) -> str:
    lowered = (details or "").lower()
    if any(marker in lowered for marker in _KEEPALIVE_MARKERS):
        return (
            "（服务端因 **keepalive ping 过于频繁**发了 GOAWAY，与并发上限无关："
            f"客户端每 {KEEPALIVE_TIME_MS // 1000}s 一次 ping，而服务端要求间隔不小于 "
            f"{MIN_PING_INTERVAL_WITHOUT_DATA_MS // 1000}s。两端版本不一致时才会出现，"
            "请把服务端升到与客户端同一版本）"
        )
    if any(marker in lowered for marker in _REFUSED_MARKERS):
        return (
            "（服务端**并发已满**并主动拒流，不是连不上：它在途的 RPC 数已达 "
            "[SERVER].max_concurrent_rpcs。请调大该项，或降低客户端并发。"
            "服务端 CPU 往往还很空闲——这是准入上限，不是算力上限）"
        )
    return port_hint(port)


_TASK_FIELDS = frozenset(
    {
        "headers",
        "cookies",
        "params",
        "data",
        "json",
        "timeout",
        "max_retries",
        "retry_backoff",
        "verify",
        "allow_redirects",
        "stream",
        "impersonate",
        "automation_config",
        "automation_script",
    }
)


class StreamedResponse:
    def __init__(self, call: Any):
        self._call: Any = call
        self._iter: Iterator[Any] = iter(call)
        self._closed: bool = False
        self._exhausted: bool = False

        self.elapsed_ms: int = 0
        self.total_bytes: int = 0
        self.trailer_error: str | None = None

        header = self._read_header()
        self.request_uuid: str = header.request_uuid
        self.url: str = header.effective_url
        self.status_code: int = header.status_code
        self.headers: dict[str, str] = dict(header.response_headers)
        self.error: str | None = header.error_message or None
        self.content_length: int = header.content_length

    def _read_header(self) -> "task_pb2.TaskRespHeader":
        try:
            first = next(self._iter)
        except grpc.RpcError as e:
            raise TransportError(f"流式下载失败: {e}") from e
        except StopIteration as e:
            raise TransportError("服务端未返回任何数据") from e

        if not first.HasField("header"):
            raise TransportError("协议错误：流的第一条消息不是 header")
        return first.header

    def is_success(self) -> bool:
        return 200 <= self.status_code < 300 and not self.error

    def __iter__(self) -> Iterator[bytes]:
        if self._exhausted:
            return
        try:
            for message in self._iter:
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

    def read(self) -> bytes:
        return b"".join(self)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._exhausted:
            self._call.cancel()

    def __enter__(self) -> "StreamedResponse":
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()

    @override
    def __repr__(self) -> str:
        return f"<StreamedResponse [{self.status_code}] {self.url}>"


class ClientBase:
    def __init__(
        self,
        config_path: str | None = None,
        host: str | None = None,
        port: int | None = None,
        token: str | None = None,
        tls: TLSSettings | None = None,
    ):
        self.config_path: str | None = config_path
        self.config: Settings = load_config(self.config_path)

        server_config: dict[str, Any] = section(self.config, "SERVER")
        self.host: str = host or server_config.get("host") or "127.0.0.1"
        self.port: int = int(port or server_config.get("port") or DEFAULT_GRPC_PORT)

        if self.host in ("[::]", "::", "0.0.0.0", ""):
            self.host = "127.0.0.1"

        security_config = section(self.config, "SECURITY")
        resolved_token = token or (load_tokens(security_config) or (None,))[0]
        self._metadata: tuple[tuple[str, str], ...] = build_client_metadata(resolved_token)

        self.tls: TLSSettings = tls or TLSSettings.from_config(security_config)
        self._credentials: grpc.ChannelCredentials | None = credentials_for(self.tls)
        self._channel_options: list[tuple[str, Any]] = client_options(self.tls)

        client_config = section(self.config, "CLIENT")
        self.rpc_max_retries: int = as_int(client_config.get("rpc_max_retries"), 2, minimum=0)
        self.rpc_retry_backoff: float = as_float(client_config.get("rpc_retry_backoff"), 0.5, minimum=0.0)
        self.compression: CompressionPolicy = CompressionPolicy(client_config)
        self._closed: bool = False

        log.debug(
            f"{type(self).__name__} 已加载配置，目标服务端 {self.host}:{self.port}，"
            f"配置节: {sorted(self.config.keys())}，"
            f"鉴权令牌: {'已配置' if self._metadata else '未配置'}，"
            f"传输层: {describe(self.tls)}，"
            f"请求压缩: {self.compression.describe()}"
        )

    @property
    def target(self) -> str:
        return f"{self.host}:{self.port}"

    def _rpc_error(self, e: grpc.RpcError) -> Exception:
        code = e.code() if hasattr(e, "code") else None
        details = e.details() if hasattr(e, "details") else str(e)
        if code is grpc.StatusCode.UNAUTHENTICATED:
            hint = "未配置令牌" if not self._metadata else "令牌不被服务端接受"
            return AuthenticationError(
                f"鉴权失败（{hint}）：{details}。"
                f"请通过环境变量 {AUTH_TOKEN_ENV}、配置 [SECURITY].auth_token "
                f"或 token=... 提供正确的令牌"
            )
        if code is grpc.StatusCode.INVALID_ARGUMENT:
            return ValidationError(f"请求参数不被服务端接受：{details}")
        if code is grpc.StatusCode.FAILED_PRECONDITION:
            return AdapterError(f"服务端无法处理该请求：{details}")
        if code is grpc.StatusCode.RESOURCE_EXHAUSTED:
            return HostLimitTimeout(f"服务端限流：{details}")
        if code is grpc.StatusCode.UNAVAILABLE:
            return TransportError(f"gRPC 调用失败 [{code}]: {details}{unavailable_hint(details, self.port)}")
        return TransportError(f"gRPC 调用失败 [{code}]: {details}")

    def _should_retry_rpc(self, error: grpc.RpcError, attempt: int) -> bool:
        if attempt >= self.rpc_max_retries:
            return False
        code = error.code() if hasattr(error, "code") else None
        return code is grpc.StatusCode.UNAVAILABLE

    def _sleep_before_retry(self, error: grpc.RpcError, attempt: int) -> None:
        delay = self.rpc_retry_backoff * (2**attempt)
        details = error.details() if hasattr(error, "details") else str(error)
        log.warning(
            f"连接服务端 {self.target} 失败（第 {attempt + 1}/{self.rpc_max_retries + 1} 次），"
            f"{delay:.1f} 秒后重试：{details}"
        )
        time.sleep(delay)

    def _build_task(
        self,
        *,
        url: str,
        adapter: IPClickAdapter | str | None = None,
        method: HttpMethod = HttpMethod.GET,
        proxy: ProxyConfig | str | bool | None = None,
        allowed_status_codes: list[int] | None = None,
        **kwargs: Any,
    ) -> DownloadTask:
        resolved_proxy: str | None
        if not proxy:
            resolved_proxy = None
        elif proxy is True:
            resolved_proxy = ProxyConfig(**proxy_config(self.config)).to_url()
            if resolved_proxy is None:
                log.warning("proxy=True 但配置文件 [PROXY] 未提供 host/tunnel_server，本次请求不走代理")
        elif isinstance(proxy, ProxyConfig):
            resolved_proxy = proxy.to_url()
        else:
            resolved_proxy = str(proxy)

        fields = {k: v for k, v in kwargs.items() if k in _TASK_FIELDS}
        passthrough = {k: v for k, v in kwargs.items() if k not in _TASK_FIELDS}

        return DownloadTask(
            adapter=adapter or IPClickAdapter.CURL_CFFI,
            url=url,
            method=method,
            proxy=resolved_proxy,
            allowed_status_codes=allowed_status_codes or [],
            kwargs=json_lib.dumps(passthrough),
            **fields,
        )

    @staticmethod
    def _deadline(task: DownloadTask) -> float:
        return task.timeout * (task.max_retries + 1) + _RPC_TIMEOUT_MARGIN


class Downloader(ClientBase):
    def __init__(
        self,
        config_path: str | None = None,
        host: str | None = None,
        port: int | None = None,
        token: str | None = None,
        tls: TLSSettings | None = None,
    ):
        super().__init__(config_path, host, port, token, tls)

        self._channel: grpc.Channel | None = None
        self._stub: task_pb2_grpc.TaskServiceStub | None = None
        self._lock: threading.Lock = threading.Lock()

    def _get_stub(self) -> task_pb2_grpc.TaskServiceStub:
        if self._closed:
            raise ClientClosedError("Downloader 已关闭，无法继续发送请求")

        if self._stub is not None:
            return self._stub

        with self._lock:
            if self._stub is None:
                self._channel = open_channel(
                    self.target,
                    credentials=self._credentials,
                    options=self._channel_options,
                    compression=grpc.Compression.Gzip,
                )
                self._stub = task_pb2_grpc.TaskServiceStub(self._channel)
        return self._stub

    def close(self) -> None:
        with self._lock:
            self._closed: bool = True
            if self._channel is not None:
                self._channel.close()
                self._channel = None
                self._stub = None

    def __enter__(self) -> "Downloader":
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()

    def request(
        self,
        *,
        adapter: IPClickAdapter | str | None = None,
        method: HttpMethod = HttpMethod.GET,
        url: str,
        headers: dict[str, Any] | None = None,
        cookies: dict[str, Any] | str | None = None,
        params: dict[str, Any] | None = None,
        data: Any = None,
        json: dict[str, Any] | None = None,
        proxy: ProxyConfig | str | bool | None = None,
        timeout: float = 60,
        max_retries: int = 3,
        retry_backoff: float = 2.0,
        verify: bool = True,
        allow_redirects: bool = True,
        stream: bool = False,
        impersonate: str | None = None,
        automation_config: str | None = None,
        automation_script: str | None = None,
        allowed_status_codes: list[int] | None = None,
        **kwargs: Any,
    ) -> DownloadResponse:
        task = self._build_task(
            url=url,
            adapter=adapter,
            method=method,
            proxy=proxy,
            allowed_status_codes=allowed_status_codes,
            headers=headers,
            cookies=cookies,
            params=params,
            data=data,
            json=json,
            timeout=timeout,
            max_retries=max_retries,
            retry_backoff=retry_backoff,
            verify=verify,
            allow_redirects=allow_redirects,
            stream=stream,
            impersonate=impersonate,
            automation_config=automation_config,
            automation_script=automation_script,
            **kwargs,
        )

        try:
            return self.download(task)
        except TransportError as e:
            log.error(f"请求 {url} 失败：{e}")
            return DownloadResponse.from_error(str(e), url=url)

    def download(self, task: DownloadTask) -> DownloadResponse:
        pb_request = task.to_protobuf()
        stub = self._get_stub()

        for attempt in range(self.rpc_max_retries + 1):
            try:
                pb_response = stub.Send(
                    pb_request,
                    timeout=self._deadline(task),
                    metadata=self._metadata or None,
                    compression=self.compression.for_request(pb_request),
                )
                return DownloadResponse.from_protobuf(pb_response)
            except grpc.RpcError as e:
                if not self._should_retry_rpc(e, attempt):
                    raise self._rpc_error(e) from e
                self._sleep_before_retry(e, attempt)
            except Exception as e:
                raise TransportError(f"连接 {self.target} 失败: {e}") from e

        raise TransportError(f"连接 {self.target} 失败：重试 {self.rpc_max_retries} 次后仍不可用")

    def stream(self, url: str, **kwargs: Any) -> StreamedResponse:
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
            return StreamedResponse(call)
        except grpc.RpcError as e:
            raise self._rpc_error(e) from e

    def batch(self, tasks: Iterable[DownloadTask], timeout: float | None = None) -> Iterator[DownloadResponse]:
        stub = self._get_stub()

        def _requests() -> Iterator[Any]:
            for task in tasks:
                yield task.to_protobuf()

        try:
            for pb_response in stub.SendBatch(
                _requests(),
                timeout=timeout,
                metadata=self._metadata or None,
                compression=self.compression.for_stream(),
            ):
                yield DownloadResponse.from_protobuf(pb_response)
        except grpc.RpcError as e:
            raise self._rpc_error(e) from e

    def get(self, url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> DownloadResponse:
        return self.request(method=HttpMethod.GET, url=url, params=params, **kwargs)

    def post(self, url: str, data: Any = None, json: dict[str, Any] | None = None, **kwargs: Any) -> DownloadResponse:
        return self.request(method=HttpMethod.POST, url=url, data=data, json=json, **kwargs)

    def put(self, url: str, data: Any = None, **kwargs: Any) -> DownloadResponse:
        return self.request(method=HttpMethod.PUT, url=url, data=data, **kwargs)

    def patch(self, url: str, data: Any = None, **kwargs: Any) -> DownloadResponse:
        return self.request(method=HttpMethod.PATCH, url=url, data=data, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> DownloadResponse:
        return self.request(method=HttpMethod.DELETE, url=url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> DownloadResponse:
        return self.request(method=HttpMethod.HEAD, url=url, **kwargs)

    def options(self, url: str, **kwargs: Any) -> DownloadResponse:
        return self.request(method=HttpMethod.OPTIONS, url=url, **kwargs)
