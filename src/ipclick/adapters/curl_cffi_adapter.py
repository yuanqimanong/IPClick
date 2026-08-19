from collections.abc import Iterator
import threading
from typing import TYPE_CHECKING, Any

from typing_extensions import override

from ipclick.adapters.base import (
    DEFAULT_CHUNK_SIZE,
    DownloaderAdapter,
    StreamEvent,
    StreamHeader,
    aretry,
    retry,
)
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

_PASSTHROUGH_KWARGS = frozenset({"ja3", "akamai", "default_headers", "http_version", "interface", "cert"})


class CurlCffiAdapter(DownloaderAdapter):
    adapter_name: str = "curl_cffi"
    supports_async: bool = True

    def __init__(self, settings: AdapterSettings | None = None):
        if _curl_cffi is None:
            raise AdapterError("curl_cffi is not installed. Install it with: pip install curl-cffi")

        super().__init__(settings)

        self.impersonate: str | None = DEFAULT_CHROME
        self.ja3: str | None = None
        self.akamai: str | None = None

        self._sessions: dict[tuple[str | None, bool, str | None], Any] = {}
        self._sessions_lock: threading.Lock = threading.Lock()
        self._async_sessions: dict[tuple[int, str | None, bool, str | None], Any] = {}

        self.ua_generator: Any = _user_agent_cls(platforms="desktop") if _user_agent_cls is not None else None

    def _get_session(self, proxy: str | None, verify: bool, impersonate: str | None) -> Any:
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
                if proxy and _curl_opt is not None:
                    session_kwargs["curl_options"] = {_curl_opt.NOPROXY: ""}
                self._sessions[key] = _curl_cffi.Session(**session_kwargs)
            return self._sessions[key]

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
        method = method.upper()
        if method not in _SUPPORTED_METHODS:
            raise ValidationError(f"Unsupported HTTP method: {method}")

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
            log.warning(f"curl_cffi request failed for {url}: {e}")
            raise

    def _get_async_session(self, proxy: str | None, verify: bool, impersonate: str | None) -> Any:
        import asyncio

        loop_key = id(asyncio.get_running_loop())
        key = (loop_key, proxy, verify, impersonate)
        session = self._async_sessions.get(key)
        if session is not None:
            return session

        session_kwargs: dict[str, Any] = {
            "proxies": self._build_proxies(proxy),
            "verify": verify,
            "impersonate": impersonate or DEFAULT_CHROME,
            "trust_env": bool(self.trust_env),
            "timeout": self.settings.download_timeout,
        }
        if proxy and _curl_opt is not None:
            session_kwargs["curl_options"] = {_curl_opt.NOPROXY: ""}
        session = _curl_cffi.AsyncSession(**session_kwargs)
        self._async_sessions[key] = session
        return session

    @override
    @aretry()
    async def adownload(self, url: str, **kwargs: Any) -> Response:
        method = str(kwargs.get("method", "GET")).upper()
        if method not in _SUPPORTED_METHODS:
            raise ValidationError(f"Unsupported HTTP method: {method}")

        request_kwargs = self._build_request_kwargs(kwargs)
        session = self._get_async_session(
            kwargs.get("proxy"), bool(kwargs.get("verify", True)), kwargs.get("impersonate")
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
        with self._sessions_lock:
            for session in self._sessions.values():
                try:
                    session.close()
                except Exception as e:
                    log.debug(f"关闭 curl_cffi session 失败: {e}")
            self._sessions.clear()


def is_available() -> bool:
    return CURL_CFFI_AVAILABLE
