import json as json_lib
import threading
from typing import Any

import grpc
from typing_extensions import override

from ipclick.auth import AUTH_TOKEN_ENV, build_client_metadata, load_tokens
from ipclick.config_loader import load_config
from ipclick.dto.models import DownloadResponse, DownloadTask, HttpMethod, IPClickAdapter, ProxyConfig
from ipclick.dto.proto import task_pb2_grpc
from ipclick.exceptions import AuthenticationError, TransportError
from ipclick.utils.config_util import Settings
from ipclick.utils.log_util import log
from ipclick.utils.secure_util import SecureUtil


# 单条消息上限（收发一致）
_MAX_MESSAGE_LENGTH = 500 * 1024 * 1024

# RPC 超时在任务超时之上留的余量（秒）：服务端还要做重试、解析和序列化，
# 如果 deadline 正好等于任务超时，客户端会先于服务端超时，拿不到错误详情。
_RPC_TIMEOUT_MARGIN = 30.0


class Downloader:
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
    ):
        """
        初始化下载器

        Args:
            config_path: 配置文件路径
            host: 服务器地址 (覆盖配置文件)
            port: 服务器端口 (覆盖配置文件)
            token: 鉴权令牌 (覆盖环境变量 ``IPCLICK_AUTH_TOKEN`` 与配置文件
                ``[SECURITY].auth_token``)。服务端未启用鉴权时可留空。
        """
        self.config_path: str | None = config_path
        self.config: Settings = load_config(self.config_path)

        # 优先级：函数参数 > 配置文件 > 内置默认值
        server_config: dict[str, Any] = dict(self.config.get("SERVER", {}))
        self.host: str = host or server_config.get("host") or "127.0.0.1"
        self.port: int = int(port or server_config.get("port") or 9527)

        # 服务端可能监听 "[::]"/"0.0.0.0"（所有网卡），但客户端不能拿它当目标地址
        if self.host in ("[::]", "::", "0.0.0.0", ""):
            self.host = "127.0.0.1"

        # 鉴权令牌：参数 > 环境变量 IPCLICK_AUTH_TOKEN > [SECURITY].auth_token
        resolved_token = token or (load_tokens(dict(self.config.get("SECURITY", {}))) or (None,))[0]
        self._metadata: tuple[tuple[str, str], ...] = build_client_metadata(resolved_token)

        self._channel: grpc.Channel | None = None
        self._stub: task_pb2_grpc.TaskServiceStub | None = None
        self._lock: threading.Lock = threading.Lock()
        self._closed: bool = False

        # 配置里含代理密码、鉴权令牌等机密，只打印结构不打印内容
        log.debug(
            f"Downloader 已加载配置，目标服务端 {self.host}:{self.port}，"
            f"配置节: {sorted(self.config.keys())}，"
            f"鉴权令牌: {'已配置' if self._metadata else '未配置'}"
        )

    # ------------------------------------------------------------------ #
    # 连接管理
    # ------------------------------------------------------------------ #

    def _get_stub(self) -> task_pb2_grpc.TaskServiceStub:
        """惰性创建并复用 channel。

        原实现每个请求都新建一个 channel，每次都要重做 TCP + HTTP/2 握手，
        并且旧 channel 在 GC 前会一直占着 fd。
        """
        if self._closed:
            raise TransportError("Downloader 已关闭，无法继续发送请求")

        if self._stub is not None:
            return self._stub

        with self._lock:
            if self._stub is None:
                self._channel = grpc.insecure_channel(
                    f"{self.host}:{self.port}",
                    options=[
                        ("grpc.max_send_message_length", _MAX_MESSAGE_LENGTH),
                        ("grpc.max_receive_message_length", _MAX_MESSAGE_LENGTH),
                        ("grpc.enable_http_proxy", 0),
                        ("grpc.keepalive_time_ms", 60000),
                        ("grpc.keepalive_timeout_ms", 30000),
                        ("grpc.keepalive_permit_without_calls", True),
                    ],
                    compression=grpc.Compression.Gzip,
                )
                self._stub = task_pb2_grpc.TaskServiceStub(self._channel)
        return self._stub

    def close(self) -> None:
        """关闭底层 gRPC channel。可重复调用。"""
        with self._lock:
            self._closed = True
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
        files: dict[str, Any] | None = None,
        proxy: ProxyConfig | str | bool | None = None,
        timeout: float = 60,
        max_retries: int = 3,
        retry_backoff: float = 2.0,
        verify: bool = True,
        allow_redirects: bool = True,
        stream: bool = False,
        impersonate: str | None = None,
        extensions: dict[str, Any] | None = None,
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
        """
        # 代理：True 表示"用配置文件里的 [PROXY]"
        resolved_proxy: str | None
        if not proxy:
            resolved_proxy = None
        elif proxy is True:
            resolved_proxy = ProxyConfig(**dict(self.config.get("PROXY", {}))).to_url()
            if resolved_proxy is None:
                log.warning("proxy=True 但配置文件 [PROXY] 未提供 host/tunnel_server，本次请求不走代理")
        elif isinstance(proxy, ProxyConfig):
            resolved_proxy = proxy.to_url()
        else:
            resolved_proxy = str(proxy)

        task = DownloadTask(
            adapter=adapter or IPClickAdapter.CURL_CFFI,
            url=url,
            method=method,
            headers=headers,
            cookies=cookies,
            params=params,
            data=data,
            json=json,
            files=files,
            proxy=resolved_proxy,
            timeout=timeout,
            max_retries=max_retries,
            retry_backoff=retry_backoff,
            verify=verify,
            allow_redirects=allow_redirects,
            stream=stream,
            impersonate=impersonate,
            extensions=extensions,
            automation_config=automation_config,
            automation_script=automation_script,
            allowed_status_codes=allowed_status_codes or [],
            kwargs=json_lib.dumps(kwargs),
        )

        try:
            return self.download(task)
        except TransportError as e:
            # 只吞传输层失败：调用方拿到的东西必须始终满足 -> DownloadResponse
            # 的签名（examples 也是这么用的）。
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
        """
        pb_request = task.to_protobuf()
        stub = self._get_stub()

        # 把任务超时传到 gRPC 层，否则服务端卡住时客户端会无限期等待
        deadline = task.timeout * (task.max_retries + 1) + _RPC_TIMEOUT_MARGIN

        try:
            pb_response = stub.Send(pb_request, timeout=deadline, metadata=self._metadata or None)
            return DownloadResponse.from_protobuf(pb_response)
        except grpc.RpcError as e:
            code = e.code() if hasattr(e, "code") else None
            details = e.details() if hasattr(e, "details") else str(e)
            # 鉴权失败重试多少次都没用，单独抛出让调用方去改令牌，
            # 而不是被 request() 当成网络失败吞成 status_code == -1 的响应。
            if code is grpc.StatusCode.UNAUTHENTICATED:
                hint = "未配置令牌" if not self._metadata else "令牌不被服务端接受"
                raise AuthenticationError(
                    f"鉴权失败（{hint}）：{details}。"
                    f"请通过环境变量 {AUTH_TOKEN_ENV}、配置 [SECURITY].auth_token "
                    f"或 Downloader(token=...) 提供正确的令牌"
                ) from e
            raise TransportError(f"gRPC 调用失败 [{code}]: {details}") from e
        except Exception as e:
            raise TransportError(f"连接 {self.host}:{self.port} 失败: {e}") from e

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
# 用普通 dict：defaultdict(Downloader) 会在任何一次误访问时凭空造出一个客户端。
_downloader_cache: dict[str, Downloader] = {}
_cache_lock = threading.Lock()


def get_downloader(config_path: str | None = None, host: str | None = None, port: int | None = None) -> Downloader:
    """获取（并缓存）下载器实例。相同参数返回同一个实例，以复用 gRPC 连接。"""
    key = SecureUtil.md5([config_path, host, port])
    downloader_instance = _downloader_cache.get(key)
    if downloader_instance is not None:
        return downloader_instance

    with _cache_lock:
        if key not in _downloader_cache:
            _downloader_cache[key] = Downloader(config_path=config_path, host=host, port=port)
        return _downloader_cache[key]


def close_all_downloaders() -> None:
    """关闭所有缓存的下载器（进程退出前调用，或在测试中隔离状态）。"""
    with _cache_lock:
        for instance in _downloader_cache.values():
            instance.close()
        _downloader_cache.clear()


class _LazyDownloader:
    """``ipclick.downloader`` 的惰性代理。

    以前这里是模块导入时就 ``Downloader()``，于是 ``import ipclick`` 会立刻
    读配置文件、打日志；服务端也 import 了 sdk，等于起服务先造一个客户端。
    改成首次真正使用时才构造。
    """

    __slots__: tuple[str, ...] = ()

    def __getattr__(self, name: str) -> Any:
        return getattr(get_downloader(), name)

    @override
    def __repr__(self) -> str:
        return "<ipclick.downloader (lazy)>"


# 向后兼容的别名：downloader.get(...) 等用法保持不变
downloader: Any = _LazyDownloader()
