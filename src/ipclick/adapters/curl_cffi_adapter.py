from collections.abc import Iterator
import threading
from typing import TYPE_CHECKING, Any

from typing_extensions import override

from ipclick.adapters.base import DEFAULT_CHUNK_SIZE, DownloaderAdapter, StreamEvent, StreamHeader, retry
from ipclick.adapters.settings import AdapterSettings
from ipclick.dto.response import Response
from ipclick.exceptions import AdapterError, ValidationError
from ipclick.utils.log_util import log


if TYPE_CHECKING:
    from curl_cffi.requests import ProxySpec

# 可选依赖：缺失时降级为 None，由 __init__ 抛 AdapterError。
# 标成 Any 是为了让"模块或 None"这种运行时形态不必到处写 type: ignore。
_curl_cffi: Any
_curl_opt: Any
_impersonate_mod: Any
_user_agent_cls: Any

try:
    from curl_cffi import CurlOpt as _curl_opt  # CurlOpt 在顶层包，不在 .requests 下
    import curl_cffi.requests as _curl_cffi
    from curl_cffi.requests import impersonate as _impersonate_mod
except ImportError:  # pragma: no cover - 取决于安装环境
    _curl_cffi = None
    _curl_opt = None
    _impersonate_mod = None

try:
    from fake_useragent import UserAgent as _user_agent_cls
except ImportError:  # pragma: no cover - 取决于安装环境
    _user_agent_cls = None

DEFAULT_CHROME: str | None = getattr(_impersonate_mod, "DEFAULT_CHROME", None)
CURL_CFFI_AVAILABLE: bool = _curl_cffi is not None
FAKE_UA_AVAILABLE: bool = _user_agent_cls is not None


_SUPPORTED_METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"})

# 允许调用方通过 kwargs 透传给 curl_cffi 的参数
_PASSTHROUGH_KWARGS = frozenset({"ja3", "akamai", "default_headers", "http_version", "interface", "cert"})


class CurlCffiAdapter(DownloaderAdapter):
    """
    curl_cffi适配器，支持浏览器指纹伪装

    优势：
    - 更好的反检测能力
    - 浏览器指纹伪装
    - 更快的性能
    - 支持HTTP/2
    """

    adapter_name: str = "curl_cffi"

    def __init__(self, settings: AdapterSettings | None = None):
        if _curl_cffi is None:
            raise AdapterError("curl_cffi is not installed. Install it with: pip install curl-cffi")

        super().__init__(settings)

        # curl_cffi特有配置
        self.impersonate: str | None = DEFAULT_CHROME
        self.ja3: str | None = None
        self.akamai: str | None = None

        # 按 (proxy, verify, impersonate) 缓存 Session，以复用连接
        self._sessions: dict[tuple[str | None, bool, str | None], Any] = {}
        self._sessions_lock: threading.Lock = threading.Lock()

        # User Agent生成器
        self.ua_generator: Any = _user_agent_cls(platforms="desktop") if _user_agent_cls is not None else None

    def _get_session(self, proxy: str | None, verify: bool, impersonate: str | None) -> Any:
        """取得（并缓存）一个 curl_cffi Session。

        原实现调用模块级的 ``curl_cffi.requests.get/post/...``，每次请求都要
        重新建连并重做 TLS 握手；``get_session()`` 虽然写了却从没被调用过。
        """
        key = (proxy, verify, impersonate)
        session = self._sessions.get(key)
        if session is not None:
            return session

        with self._sessions_lock:
            if key not in self._sessions:
                session_kwargs: dict[str, Any] = {
                    "proxies": self._build_proxies(proxy),
                    "verify": verify,
                    "impersonate": impersonate or DEFAULT_CHROME,
                    "trust_env": bool(self.trust_env),
                    "timeout": self.settings.download_timeout,
                }
                # 调用方显式指定了代理时，必须同时清空 no-proxy 列表。
                # libcurl 会自行读取环境里的 no_proxy/NO_PROXY，命中的目标会
                # 绕过我们设置的代理直连并返回 200——代理被静默丢弃，
                # 调用方还以为走了代理。空字符串表示"没有任何主机免代理"。
                if proxy and _curl_opt is not None:
                    session_kwargs["curl_options"] = {_curl_opt.NOPROXY: ""}
                self._sessions[key] = _curl_cffi.Session(**session_kwargs)
            return self._sessions[key]

    def _build_proxies(self, proxy: str | None) -> "ProxySpec | None":
        """构造 curl_cffi 的 proxies 参数。

        libcurl 会自己读环境变量里的 http_proxy/https_proxy，Session 上的
        ``trust_env=False`` 并不能阻止它（实测 proxies=None 和 proxies={} 都
        仍然走环境代理）。只有显式传空字符串才能真正关掉，否则"不指定代理"
        会静默变成"走服务端所在机器的环境代理"。
        """
        if proxy:
            return {"http": proxy, "https": proxy}
        if self.trust_env:
            return None
        return {"http": "", "https": ""}

    @override
    @retry()
    def download(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, Any] | None = None,
        cookies: dict[str, Any] | str | None = None,
        params: dict[str, Any] | None = None,
        data: Any = None,
        json: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        proxy: str | None = None,
        timeout: float = 60,
        max_retries: int = 3,
        retry_delay: float = 2.0,
        verify: bool = True,
        allow_redirects: bool = True,
        stream: bool = False,
        impersonate: str | None = None,
        extensions: dict[str, Any] | None = None,
        automation_config: str | None = None,
        automation_script: str | None = None,
        allowed_status_codes: list[int] | None = None,
        kwargs: str | None = None,
    ) -> Response:
        """
        使用curl_cffi执行HTTP请求
        """
        method = method.upper()
        if method not in _SUPPORTED_METHODS:
            raise ValidationError(f"Unsupported HTTP method: {method}")

        # 以前这里是无条件 json.loads(kwargs)，kwargs 为空串时直接抛
        # JSONDecodeError，而且解析结果压根没被用到。
        extra = self.parse_extra_kwargs(kwargs)

        request_kwargs: dict[str, Any] = {
            "headers": headers,
            "cookies": cookies,
            "params": params,
            "data": data,
            "json": json,
            "timeout": timeout or self.timeout,
            "allow_redirects": allow_redirects,
            # 不转发 stream：curl_cffi 在 stream=True 时返回未消费的流式响应，
            # 我们随后读 .content 得到的是 b''，而 status_code 仍是 200、
            # exception 仍是 None——调用方完全无从察觉整个响应体已经丢失。
            # 服务端本来就要把响应体整个塞进一条 protobuf 消息，没有真正的流式
            # 通路可言（见 README「尚未实现」），所以这里与 httpx 适配器保持一致：
            # 忽略该参数，等真正支持 server-streaming RPC 时再一并实现。
        }
        for key in _PASSTHROUGH_KWARGS:
            if key in extra:
                request_kwargs[key] = extra[key]

        request_kwargs = {k: v for k, v in request_kwargs.items() if v is not None}

        session = self._get_session(proxy, verify, impersonate)

        try:
            curl_cffi_resp = session.request(method, url, **request_kwargs)

            return Response(
                url=str(curl_cffi_resp.url),
                status_code=curl_cffi_resp.status_code,
                content=curl_cffi_resp.content,
                text=curl_cffi_resp.text,
                headers=dict(curl_cffi_resp.headers),
                raw_response=curl_cffi_resp,
            )

        except Exception as e:
            # 不打完整堆栈：retry 会重试多次，每次一份 traceback 会淹没日志。
            log.warning(f"curl_cffi request failed for {url}: {e}")
            raise

    @override
    def download_stream(
        self,
        url: str,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        **kwargs: Any,
    ) -> Iterator[StreamEvent]:
        """curl_cffi 的真流式实现。

        这里是 stream=True 唯一正确的用法：必须在响应上下文里把分片消费完。
        之前在 download() 里转发 stream=True 却又去读 .content，拿到的是空
        bytes 而 status_code 仍是 200——静默丢包，所以那条路径已经把该参数
        彻底忽略掉了。
        """
        method = str(kwargs.get("method", "GET")).upper()
        if method not in _SUPPORTED_METHODS:
            yield StreamHeader(url=url, status_code=-1, error=f"Unsupported HTTP method: {method}")
            return

        session = self._get_session(kwargs.get("proxy"), bool(kwargs.get("verify", True)), kwargs.get("impersonate"))

        request_kwargs: dict[str, Any] = {
            "headers": kwargs.get("headers"),
            "cookies": kwargs.get("cookies"),
            "params": kwargs.get("params"),
            "data": kwargs.get("data"),
            "json": kwargs.get("json"),
            "timeout": kwargs.get("timeout") or self.timeout,
            "allow_redirects": bool(kwargs.get("allow_redirects", True)),
        }
        request_kwargs = {k: v for k, v in request_kwargs.items() if v is not None}

        try:
            response = session.request(method, url, stream=True, **request_kwargs)
        except Exception as e:
            log.warning(f"curl_cffi stream failed for {url}: {e}")
            yield StreamHeader(url=url, status_code=-1, error=str(e))
            return

        try:
            yield StreamHeader(
                url=str(response.url),
                status_code=response.status_code,
                headers=dict(response.headers),
                content_length=int(response.headers.get("content-length") or -1),
            )
            yield from response.iter_content(chunk_size=chunk_size)
        finally:
            response.close()

    @override
    def close(self) -> None:
        """关闭所有缓存的 Session"""
        with self._sessions_lock:
            for session in self._sessions.values():
                try:
                    session.close()
                except Exception as e:
                    log.debug(f"关闭 curl_cffi session 失败: {e}")
            self._sessions.clear()


def is_available() -> bool:
    """检查curl_cffi是否可用"""
    return CURL_CFFI_AVAILABLE
