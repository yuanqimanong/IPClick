import threading
from typing import TYPE_CHECKING, Any

from typing_extensions import override

from ipclick.adapters.base import DownloaderAdapter, retry
from ipclick.dto.response import Response
from ipclick.exceptions import AdapterError
from ipclick.utils.log_util import log


if TYPE_CHECKING:
    from curl_cffi.requests import ProxySpec

# 可选依赖：缺失时降级为 None，由 __init__ 抛 AdapterError。
# 标成 Any 是为了让"模块或 None"这种运行时形态不必到处写 type: ignore。
_curl_cffi: Any
_impersonate_mod: Any
_user_agent_cls: Any

try:
    import curl_cffi.requests as _curl_cffi
    from curl_cffi.requests import impersonate as _impersonate_mod
except ImportError:  # pragma: no cover - 取决于安装环境
    _curl_cffi = None
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

    def __init__(self):
        if _curl_cffi is None:
            raise AdapterError("curl_cffi is not installed. Install it with: pip install curl-cffi")

        super().__init__()

        # curl_cffi特有配置
        self.impersonate: str | None = DEFAULT_CHROME
        self.ja3: str | None = None
        self.akamai: str | None = None

        # 按 (proxy, verify, impersonate) 缓存 Session，以复用连接
        self._sessions: dict[tuple[str | None, bool, str | None], Any] = {}
        self._sessions_lock: threading.Lock = threading.Lock()
        # 是否读取环境变量里的代理配置，默认关闭：代理应由调用方显式指定，
        # 而不是取决于服务端所在机器的 HTTP_PROXY/ALL_PROXY
        self.trust_env: bool = False

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
                self._sessions[key] = _curl_cffi.Session(
                    proxies=self._build_proxies(proxy),
                    verify=verify,
                    impersonate=impersonate or DEFAULT_CHROME,
                    trust_env=bool(self.trust_env),
                )
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
            raise AdapterError(f"Unsupported HTTP method: {method}")

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
            "stream": stream,
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
