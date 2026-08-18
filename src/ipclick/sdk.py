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
from ipclick.dto.proto import task_pb2_grpc
from ipclick.exceptions import (
    AdapterError,
    AuthenticationError,
    ClientClosedError,
    TransportError,
    ValidationError,
)
from ipclick.limiter import HostLimitTimeout
from ipclick.ports import DEFAULT_GRPC_PORT, port_hint
from ipclick.secrets import proxy_config
from ipclick.tls import TLSSettings, channel_credentials, channel_options, describe
from ipclick.utils.config_util import Settings
from ipclick.utils.log_util import log


# 单条消息上限（收发一致）
_MAX_MESSAGE_LENGTH = 500 * 1024 * 1024

# RPC 超时在任务超时之上留的余量（秒）：服务端还要做重试、解析和序列化，
# 如果 deadline 正好等于任务超时，客户端会先于服务端超时，拿不到错误详情。
_RPC_TIMEOUT_MARGIN = 30.0


def _as_int(value: Any, default: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result if result >= 0 else default


def _as_float(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result >= 0 else default


#: gRPC channel 选项，同步与异步客户端共用
CHANNEL_OPTIONS: list[tuple[str, Any]] = [
    ("grpc.max_send_message_length", _MAX_MESSAGE_LENGTH),
    ("grpc.max_receive_message_length", _MAX_MESSAGE_LENGTH),
    # 不读环境里的 http_proxy：目标是本项目自己的服务端，不该被环境代理劫走
    ("grpc.enable_http_proxy", 0),
    ("grpc.keepalive_time_ms", 60000),
    ("grpc.keepalive_timeout_ms", 30000),
    ("grpc.keepalive_permit_without_calls", True),
]

#: DownloadTask 直接认识的字段；其余关键字参数作为 kwargs 透传给底层 HTTP 客户端
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
    """流式下载的响应句柄。

    构造时会先把第一条 header 消息读出来，因此 ``status_code`` / ``headers``
    在开始迭代 body 之前就可用——调用方可以据此决定继续接收还是直接放弃，
    不必先把整个响应体拉完。

    用完请调用 :meth:`close`，或直接当上下文管理器用（提前退出会 cancel 掉
    这条 gRPC 流，服务端也就不会继续白白下载了）。
    """

    def __init__(self, call: Any):
        self._call: Any = call
        self._iter: Iterator[Any] = iter(call)
        self._closed: bool = False
        self._exhausted: bool = False

        #: 以下三项要等 trailer 到达（即 body 读完）之后才有值
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

    def _read_header(self) -> Any:
        """第一条消息必须是 header，否则协议就被破坏了。"""
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
        """迭代响应体分片。trailer 到达后自动停止并记录统计信息。"""
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
        """把剩余分片全部读进内存。

        这等于放弃了流式的意义，只适合小响应或测试。大文件请直接迭代。
        """
        return b"".join(self)

    def close(self) -> None:
        """取消这条流。已经读完的话是 no-op。"""
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
    """同步与异步客户端共享的配置、令牌与任务组装逻辑。

    抽出来是为了让 :class:`Downloader` 和 :class:`~ipclick.aio.AsyncDownloader`
    对同样的输入产生同样的 DownloadTask——代理解析、令牌优先级这些规则
    在两处各写一份的话迟早会失步。
    """

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

        # 优先级：函数参数 > 配置文件 > 内置默认值
        server_config: dict[str, Any] = dict(self.config.get("SERVER", {}))
        self.host: str = host or server_config.get("host") or "127.0.0.1"
        self.port: int = int(port or server_config.get("port") or DEFAULT_GRPC_PORT)

        # 服务端可能监听 "[::]"/"0.0.0.0"（所有网卡），但客户端不能拿它当目标地址
        if self.host in ("[::]", "::", "0.0.0.0", ""):
            self.host = "127.0.0.1"

        # 鉴权令牌：参数 > 环境变量 IPCLICK_AUTH_TOKEN > [SECURITY].auth_token
        security_config = dict(self.config.get("SECURITY", {}))
        resolved_token = token or (load_tokens(security_config) or (None,))[0]
        self._metadata: tuple[tuple[str, str], ...] = build_client_metadata(resolved_token)

        # 传输层加密。凭据在这里就构造出来——证书路径写错要在建连之前失败，
        # 而不是等到第一个请求发出去才报一个含糊的连接错误。
        self.tls: TLSSettings = tls or TLSSettings.from_config(security_config)
        self._credentials: grpc.ChannelCredentials | None = channel_credentials(self.tls) if self.tls.enabled else None
        self._channel_options: list[tuple[str, Any]] = CHANNEL_OPTIONS + channel_options(self.tls)

        # 客户端到服务端这一跳的重试。适配器内部的重试解决的是"目标站点抖了"，
        # 这里解决的是"我们自己的服务端抖了"，两者互不覆盖。
        client_config = dict(self.config.get("CLIENT", {}))
        self.rpc_max_retries: int = _as_int(client_config.get("rpc_max_retries"), 2)
        self.rpc_retry_backoff: float = _as_float(client_config.get("rpc_retry_backoff"), 0.5)
        # 请求压缩。自动化脚本是主要动机：整个脚本文件走线，压完通常只剩几十分之一。
        self.compression: CompressionPolicy = CompressionPolicy(client_config)
        # 子类的 close() 会改写它，声明在这里以便类型检查器识别
        self._closed: bool = False

        # 配置里含代理密码、鉴权令牌等机密，只打印结构不打印内容
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
        """把 gRPC 错误翻译成本项目的异常类型。"""
        code = e.code() if hasattr(e, "code") else None
        details = e.details() if hasattr(e, "details") else str(e)
        # 鉴权失败重试多少次都没用，单独抛出让调用方去改令牌，
        # 而不是被 request() 当成网络失败吞成 status_code == -1 的响应。
        if code is grpc.StatusCode.UNAUTHENTICATED:
            hint = "未配置令牌" if not self._metadata else "令牌不被服务端接受"
            return AuthenticationError(
                f"鉴权失败（{hint}）：{details}。"
                f"请通过环境变量 {AUTH_TOKEN_ENV}、配置 [SECURITY].auth_token "
                f"或 token=... 提供正确的令牌"
            )
        # 参数错误同理：换成 -1 响应会让调用方去查网络，而真正要改的是自己的
        # 调用参数。客户端本地发现的参数错误早就是抛出的，服务端发现的没理由不一致。
        if code is grpc.StatusCode.INVALID_ARGUMENT:
            return ValidationError(f"请求参数不被服务端接受：{details}")
        # "这个服务端做不到"——适配器没实现、可选依赖没装、浏览器渲染被关掉。
        # 改参数没用，得改服务端部署，所以也不该伪装成一次网络抖动。
        if code is grpc.StatusCode.FAILED_PRECONDITION:
            return AdapterError(f"服务端无法处理该请求：{details}")
        # 服务端的按 host 限流生效了。同样不是网络问题——要么降低发送速率，
        # 要么调大 [DOWNLOADER] 里的 per_host 限额。
        if code is grpc.StatusCode.RESOURCE_EXHAUSTED:
            return HostLimitTimeout(f"服务端限流：{details}")
        # 连不上时附一句端口提示。0.5.0 把 9527 让给了 Web 管理端，从 0.4 升上来
        # 的调用方仍然连 9527 就会握到一个 HTTP 服务上——gRPC 报出来的错误
        # （UNAVAILABLE / 握手失败）和"端口变了"这件事看不出任何关系。
        if code is grpc.StatusCode.UNAVAILABLE:
            return TransportError(f"gRPC 调用失败 [{code}]: {details}{port_hint(self.port)}")
        return TransportError(f"gRPC 调用失败 [{code}]: {details}")

    def _should_retry_rpc(self, error: grpc.RpcError, attempt: int) -> bool:
        """这次 RPC 失败能不能安全重试。

        只认 UNAVAILABLE：那意味着**连接就没建起来**，请求根本没到过服务端，
        重发不会造成重复执行。

        DEADLINE_EXCEEDED 刻意不重试——请求可能已经发出去、服务端正在执行，
        只是回复没赶上。这时重发一个 POST 就是重复下单。宁可让调用方拿到超时
        自己决定，也不替它做这个决定。
        """
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
        """把调用方参数组装成 DownloadTask。"""
        # 代理：True 表示"用配置文件里的 [PROXY]"
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
        """RPC deadline：任务超时 × 尝试次数，再留一点服务端处理余量。

        不设的话服务端卡住时客户端会无限期等待。
        """
        return task.timeout * (task.max_retries + 1) + _RPC_TIMEOUT_MARGIN


class Downloader(ClientBase):
    """IPClick下载器客户端

    实例持有一个可复用的 gRPC channel。用完请调用 :meth:`close`，
    或直接当作上下文管理器使用::

        with Downloader() as d:
            resp = d.get("https://example.com")
    """

    def __init__(
        self,
        config_path: str | None = None,
        host: str | None = None,
        port: int | None = None,
        token: str | None = None,
        tls: TLSSettings | None = None,
    ):
        """
        初始化下载器

        Args:
            config_path: 配置文件路径
            host: 服务器地址 (覆盖配置文件)
            port: 服务器端口 (覆盖配置文件)
            token: 鉴权令牌 (覆盖环境变量 ``IPCLICK_AUTH_TOKEN`` 与配置文件
                ``[SECURITY].auth_token``)。服务端未启用鉴权时可留空。
            tls: 传输层配置 (覆盖 ``[SECURITY.tls]``)。服务端启用 TLS 时必须
                同步开启，否则连接会握手失败。
        """
        super().__init__(config_path, host, port, token, tls)

        self._channel: grpc.Channel | None = None
        self._stub: task_pb2_grpc.TaskServiceStub | None = None
        self._lock: threading.Lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # 连接管理
    # ------------------------------------------------------------------ #

    def _get_stub(self) -> task_pb2_grpc.TaskServiceStub:
        """惰性创建并复用 channel。

        原实现每个请求都新建一个 channel，每次都要重做 TCP + HTTP/2 握手，
        并且旧 channel 在 GC 前会一直占着 fd。
        """
        if self._closed:
            raise ClientClosedError("Downloader 已关闭，无法继续发送请求")

        if self._stub is not None:
            return self._stub

        with self._lock:
            if self._stub is None:
                self._channel = (
                    grpc.secure_channel(
                        self.target,
                        self._credentials,
                        options=self._channel_options,
                        compression=grpc.Compression.Gzip,
                    )
                    if self._credentials is not None
                    else grpc.insecure_channel(
                        self.target,
                        options=self._channel_options,
                        compression=grpc.Compression.Gzip,
                    )
                )
                self._stub = task_pb2_grpc.TaskServiceStub(self._channel)
        return self._stub

    def close(self) -> None:
        """关闭底层 gRPC channel。可重复调用。"""
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

    # ------------------------------------------------------------------ #
    # 请求
    # ------------------------------------------------------------------ #

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
        """发送一次下载请求。

        本方法不会抛出网络异常：失败时返回 ``status_code == -1`` 且 ``error``
        非空的 :class:`DownloadResponse`。参数本身非法（如 URL 为空）仍会抛
        :class:`~ipclick.exceptions.ValidationError`。

        Args:
            verify: 是否校验 SSL 证书，默认 True。
                （0.2.0 之前这里默认 None，会被 protobuf 当成"未设置"，
                服务端因而收到 False——即默认关闭证书校验。）

        Note:
            没有 ``files`` 参数。协议里从来没有这个字段（旧版的 ``files=``
            一律抛 NotImplementedError），所以删掉它只是把 API 说实话。
            要上传文件请自己拼好 multipart 体，用 ``data=<bytes>`` 加上
            ``Content-Type: multipart/form-data; boundary=...`` 头发出去——
            ``data`` 现在是 bytes 字段，任意二进制都能原样送达。
        """
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
            # 只吞传输层失败：调用方拿到的东西必须始终满足 -> DownloadResponse
            # 的签名（Wiki 的「SDK 用法」也是这么用的）。
            #
            # 注意这里不能写 `except IPClickError`——ValidationError 也是它的子类，
            # 于是「适配器名拼错」这类参数错误会被伪装成 status_code == -1 的
            # 网络失败，调用方对着网络排查半天也找不到原因。参数错误必须抛出。
            log.error(f"请求 {url} 失败：{e}")
            return DownloadResponse.from_error(str(e), url=url)

    def download(self, task: DownloadTask) -> DownloadResponse:
        """
        执行下载任务

        Args:
            task: 下载任务对象

        Returns:
            下载响应对象

        Raises:
            TransportError: 与服务端通信失败。
            AuthenticationError: 鉴权失败。
        """
        pb_request = task.to_protobuf()
        stub = self._get_stub()

        for attempt in range(self.rpc_max_retries + 1):
            try:
                pb_response = stub.Send(
                    pb_request,
                    timeout=self._deadline(task),
                    metadata=self._metadata or None,
                    # 带自动化脚本的请求压完通常只剩几十分之一；小请求会被
                    # 策略跳过，不白费 CPU。见 ipclick.compression。
                    compression=self.compression.for_request(pb_request),
                )
                return DownloadResponse.from_protobuf(pb_response)
            except grpc.RpcError as e:
                if not self._should_retry_rpc(e, attempt):
                    raise self._rpc_error(e) from e
                self._sleep_before_retry(e, attempt)
            except Exception as e:
                raise TransportError(f"连接 {self.target} 失败: {e}") from e

        # for 正常走完只可能是最后一次也重试失败了，而那一次 _should_retry_rpc
        # 会返回 False 并抛出——所以这里理论上不可达，留个明确的错误兜底。
        raise TransportError(f"连接 {self.target} 失败：重试 {self.rpc_max_retries} 次后仍不可用")

    # ------------------------------------------------------------------ #
    # 流式下载
    # ------------------------------------------------------------------ #

    def stream(self, url: str, **kwargs: Any) -> StreamedResponse:
        """流式下载：响应体分片返回，全程不把整个 body 放进内存。

        大文件请用这个而不是 :meth:`get`。返回对象先给出状态码与响应头，
        再按需迭代分片::

            with downloader.stream("https://example.com/big.zip") as resp:
                print(resp.status_code, resp.headers)
                with open("big.zip", "wb") as f:
                    for chunk in resp:
                        f.write(chunk)

        Raises:
            TransportError / AuthenticationError: 与服务端通信失败。
            ValidationError: 参数非法。
        """
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

    # ------------------------------------------------------------------ #
    # 批量
    # ------------------------------------------------------------------ #

    def batch(self, tasks: Iterable[DownloadTask], timeout: float | None = None) -> Iterator[DownloadResponse]:
        """批量下载：一次 RPC 处理多个任务。

        结果按**完成顺序**产出，不是提交顺序——慢的那个不会挡住快的，
        所以要靠 ``request_uuid`` 对应回请求，不能靠顺序。
        单个任务失败不影响其他任务，失败信息在各自响应的 ``error`` 里。

        ::

            tasks = [DownloadTask(uuid=u, url=u) for u in urls]
            for resp in downloader.batch(tasks):
                print(resp.request_uuid, resp.status_code)
        """
        stub = self._get_stub()

        def _requests() -> Iterator[Any]:
            for task in tasks:
                yield task.to_protobuf()

        try:
            # 批量走流式，压缩只能在整条流上设一次（不能逐条判定），
            # 所以这里按模式来：auto 也压——批量本来就是"量大"的场景。
            for pb_response in stub.SendBatch(
                _requests(),
                timeout=timeout,
                metadata=self._metadata or None,
                compression=self.compression.for_stream(),
            ):
                yield DownloadResponse.from_protobuf(pb_response)
        except grpc.RpcError as e:
            raise self._rpc_error(e) from e

    # ------------------------------------------------------------------ #
    # 便捷方法
    # ------------------------------------------------------------------ #

    def get(self, url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> DownloadResponse:
        """发送GET请求"""
        return self.request(method=HttpMethod.GET, url=url, params=params, **kwargs)

    def post(self, url: str, data: Any = None, json: dict[str, Any] | None = None, **kwargs: Any) -> DownloadResponse:
        """发送POST请求"""
        return self.request(method=HttpMethod.POST, url=url, data=data, json=json, **kwargs)

    def put(self, url: str, data: Any = None, **kwargs: Any) -> DownloadResponse:
        """发送PUT请求"""
        return self.request(method=HttpMethod.PUT, url=url, data=data, **kwargs)

    def patch(self, url: str, data: Any = None, **kwargs: Any) -> DownloadResponse:
        """发送PATCH请求"""
        return self.request(method=HttpMethod.PATCH, url=url, data=data, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> DownloadResponse:
        """发送DELETE请求"""
        return self.request(method=HttpMethod.DELETE, url=url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> DownloadResponse:
        """发送HEAD请求"""
        return self.request(method=HttpMethod.HEAD, url=url, **kwargs)

    def options(self, url: str, **kwargs: Any) -> DownloadResponse:
        """发送OPTIONS请求"""
        return self.request(method=HttpMethod.OPTIONS, url=url, **kwargs)


# 按 (config_path, host, port) 缓存的下载器实例。
