"""基于 curl_cffi 的同步、异步和流式 HTTP 适配器。"""

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from typing_extensions import override

from ipclick.adapters.base import DEFAULT_CHUNK_SIZE, DownloaderAdapter, StreamEvent, StreamHeader
from ipclick.adapters.redirects import follow_with_policy
from ipclick.adapters.retry import aretry, retry
from ipclick.adapters.sessions import AsyncSessionCache, SessionCache
from ipclick.adapters.settings import AdapterSettings
from ipclick.dto.response import Response
from ipclick.exceptions import AdapterError, ValidationError
from ipclick.utils.log_util import log


if TYPE_CHECKING:
    from curl_cffi.requests import ProxySpec

_curl_cffi: Any
_curl_opt: Any
_impersonate_mod: Any
_user_agent_cls: Any

try:
    from curl_cffi import CurlOpt as _curl_opt
    import curl_cffi.requests as _curl_cffi
    from curl_cffi.requests import impersonate as _impersonate_mod
except ImportError:
    _curl_cffi = None
    _curl_opt = None
    _impersonate_mod = None

try:
    from fake_useragent import UserAgent as _user_agent_cls
except ImportError:
    _user_agent_cls = None

DEFAULT_CHROME: str | None = getattr(_impersonate_mod, "DEFAULT_CHROME", None)
CURL_CFFI_AVAILABLE: bool = _curl_cffi is not None
FAKE_UA_AVAILABLE: bool = _user_agent_cls is not None


_SUPPORTED_METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"})

SessionKey = tuple[str | None, bool, str | None]

_PASSTHROUGH_KWARGS = frozenset({"ja3", "akamai", "default_headers", "http_version", "interface", "cert"})


class CurlCffiAdapter(DownloaderAdapter):
    """复用 curl_cffi session 并支持 TLS 指纹伪装的适配器。"""

    adapter_name: str = "curl_cffi"
    supports_async: bool = True

    def __init__(self, settings: AdapterSettings | None = None):
        """校验可选依赖并初始化同步、异步 session 缓存。"""
        if _curl_cffi is None:
            raise AdapterError("curl_cffi is not installed. Install it with: pip install curl-cffi")

        super().__init__(settings)

        self.impersonate: str | None = DEFAULT_CHROME
        self.ja3: str | None = None
        self.akamai: str | None = None

        self._sessions: SessionCache[SessionKey] = SessionCache(self.adapter_name, self._new_session)
        self._async_sessions: AsyncSessionCache[SessionKey] = AsyncSessionCache(
            self.adapter_name, self._new_async_session
        )

        self.ua_generator: Any = _user_agent_cls(platforms="desktop") if _user_agent_cls is not None else None

    def _session_kwargs(self, key: SessionKey) -> dict[str, Any]:
        proxy, verify, impersonate = key
        kwargs: dict[str, Any] = {
            "proxies": self._build_proxies(proxy),
            "verify": verify,
            "impersonate": impersonate or DEFAULT_CHROME,
            "trust_env": bool(self.trust_env),
            "timeout": self.settings.download_timeout,
        }
        if proxy and _curl_opt is not None:
            kwargs["curl_options"] = {_curl_opt.NOPROXY: ""}
        return kwargs

    def _new_session(self, key: SessionKey) -> Any:
        return _curl_cffi.Session(**self._session_kwargs(key))

    def _new_async_session(self, key: SessionKey) -> Any:
        return _curl_cffi.AsyncSession(max_clients=self.settings.max_connections, **self._session_kwargs(key))

    def _build_proxies(self, proxy: str | None) -> "ProxySpec | None":
        if proxy:
            return {"http": proxy, "https": proxy}
        if self.trust_env:
            return None
        return {"http": "", "https": ""}

    def _build_request_kwargs(self, source: dict[str, Any]) -> dict[str, Any]:
        extra = self.parse_extra_kwargs(source.get("kwargs"))

        built: dict[str, Any] = {
            "headers": source.get("headers"),
            "cookies": source.get("cookies"),
            "params": source.get("params"),
            "data": source.get("data"),
            "json": source.get("json"),
            "timeout": source.get("timeout") or self.timeout,
            "allow_redirects": source.get("allow_redirects", True),
        }
        for key in _PASSTHROUGH_KWARGS:
            if key in extra:
                built[key] = extra[key]
        return {k: v for k, v in built.items() if v is not None}

    def _request_following_policy(
        self,
        session: Any,
        method: str,
        url: str,
        request_kwargs: dict[str, Any],
        allow_redirects: bool,
    ) -> Any:
        """发一次请求；装了 url_validator 时逐跳跟随重定向并逐跳校验。

        默认让 libcurl 自己跟随重定向，跟随时不会再过 SSRF 准入——一次
        ``302 Location: http://169.254.169.254/`` 就能把云元数据取回来。所以服务端
        注入了校验器时改成自己跟随，每跳**发出之前**校验一次。
        """
        validator = self.url_validator
        if validator is None or not allow_redirects:
            return session.request(method, url, **request_kwargs)

        hop_kwargs = dict(request_kwargs)
        hop_kwargs["allow_redirects"] = False
        body_keys = ("data", "json", "files")
        saved_body = {key: hop_kwargs.get(key) for key in body_keys}

        def send(hop_url: str, hop_method: str, body: Any) -> Any:
            kw = dict(hop_kwargs)
            if body is None:
                for key in body_keys:
                    _ = kw.pop(key, None)
            return session.request(hop_method, hop_url, **kw)

        return follow_with_policy(send, url, method, saved_body, validator)

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
        automation_config: str | None = None,
        automation_script: str | None = None,
        allowed_status_codes: list[int] | None = None,
        kwargs: str | None = None,
    ) -> Response:
        """同步发送 HTTP 请求并转换为统一响应。"""
        method = method.upper()
        if method not in _SUPPORTED_METHODS:
            raise ValidationError(f"Unsupported HTTP method: {method}")
        if files:
            # gRPC 上没有 files 字段，所以这只可能来自进程内直接调用。
            # 之前是静默丢掉——请求照样发出，body 是空的，调用方查不出原因。
            raise ValidationError(
                "curl_cffi 适配器不支持 files 参数：请自行拼好 multipart 请求体，"
                "用 data=<bytes> 加上 Content-Type: multipart/form-data; boundary=... 发送"
            )

        request_kwargs = self._build_request_kwargs(
            {
                "headers": headers,
                "cookies": cookies,
                "params": params,
                "data": data,
                "json": json,
                "timeout": timeout,
                "allow_redirects": allow_redirects,
                "kwargs": kwargs,
            }
        )

        # proxy/证书校验/指纹会改变连接属性，必须分开复用连接池。
        session = self._sessions.get((proxy, verify, impersonate))

        try:
            curl_cffi_resp = self._request_following_policy(session, method, url, request_kwargs, allow_redirects)

            return Response(
                url=str(curl_cffi_resp.url),
                status_code=curl_cffi_resp.status_code,
                content=curl_cffi_resp.content,
                text=curl_cffi_resp.text,
                headers=dict(curl_cffi_resp.headers),
                raw_response=curl_cffi_resp,
            )

        except Exception as e:
            log.warning(f"curl_cffi request failed for {url}: {e}")
            raise

    @override
    @aretry()
    async def adownload(self, url: str, **kwargs: Any) -> Response:
        """使用原生 AsyncSession 异步发送 HTTP 请求。"""
        method = str(kwargs.get("method", "GET")).upper()
        if method not in _SUPPORTED_METHODS:
            raise ValidationError(f"Unsupported HTTP method: {method}")

        request_kwargs = self._build_request_kwargs(kwargs)
        session = self._async_sessions.get(
            (kwargs.get("proxy"), bool(kwargs.get("verify", True)), kwargs.get("impersonate"))
        )
        try:
            resp = await session.request(method, url, **request_kwargs)
            return Response(
                url=str(resp.url),
                status_code=resp.status_code,
                content=resp.content,
                text=resp.text,
                headers=dict(resp.headers),
                raw_response=resp,
            )
        except Exception as e:
            log.warning(f"curl_cffi async request failed for {url}: {e}")
            raise

    @override
    def download_stream(
        self,
        url: str,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        **kwargs: Any,
    ) -> Iterator[StreamEvent]:
        """产出响应首部及正文分片，并在生成器结束时关闭响应。"""
        method = str(kwargs.get("method", "GET")).upper()
        if method not in _SUPPORTED_METHODS:
            yield StreamHeader(url=url, status_code=-1, error=f"Unsupported HTTP method: {method}")
            return

        session = self._sessions.get((kwargs.get("proxy"), bool(kwargs.get("verify", True)), kwargs.get("impersonate")))

        # 与普通请求共用白名单 builder，避免切换到 stream 后静默丢失指纹、证书等参数。
        request_kwargs = self._build_request_kwargs(kwargs)

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
            # 提前停止迭代也会执行 finally，归还底层连接。
            response.close()

    @override
    def close(self) -> None:
        """关闭同步缓存，并调度关闭异步 session。"""
        self._sessions.close()
        self._async_sessions.close()

    @override
    async def aclose(self) -> None:
        """在正确事件循环中关闭所有 session。"""
        self._sessions.close()
        await self._async_sessions.aclose()


def is_available() -> bool:
    """返回 curl_cffi 是否已安装。"""
    return CURL_CFFI_AVAILABLE
