from __future__ import annotations

from collections.abc import Iterator
import threading
from typing import Any

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


_UNPROBED: Any = object()
_niquests: Any = _UNPROBED
_user_agent_cls: Any = _UNPROBED

NIQUESTS_MODULE = "niquests"


def _load_niquests() -> Any:
    global _niquests
    if _niquests is _UNPROBED:
        try:
            import niquests

            _niquests = niquests
        except ImportError:
            _niquests = None
    return _niquests


def _load_user_agent() -> Any:
    global _user_agent_cls
    if _user_agent_cls is _UNPROBED:
        try:
            from fake_useragent import UserAgent

            _user_agent_cls = UserAgent
        except ImportError:
            _user_agent_cls = None
    return _user_agent_cls


_SUPPORTED_METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"})

_PASSTHROUGH_KWARGS = frozenset({"auth", "cert", "hooks"})


class NiquestsAdapter(DownloaderAdapter):
    adapter_name: str = "niquests"
    supports_async: bool = True

    def __init__(self, settings: AdapterSettings | None = None):
        if _load_niquests() is None:
            raise AdapterError('niquests is not installed. Install it with: pip install "ipclick[niquests]"')

        super().__init__(settings)
        self._sessions: dict[tuple[str | None, bool], Any] = {}
        self._sessions_lock: threading.Lock = threading.Lock()
        self._async_sessions: dict[tuple[int, str | None, bool], Any] = {}
        user_agent = _load_user_agent()
        self.ua_generator: Any = user_agent(platforms="desktop") if user_agent is not None else None

    def _get_session(self, proxy: str | None, verify: bool) -> Any:
        key = (proxy, verify)
        session = self._sessions.get(key)
        if session is not None:
            return session

        with self._sessions_lock:
            if key not in self._sessions:
                session = _load_niquests().Session()
                session.verify = verify
                session.trust_env = bool(self.trust_env)
                if proxy:
                    session.proxies = {"http": proxy, "https": proxy}
                adapter = _load_niquests().adapters.HTTPAdapter(
                    pool_connections=self.settings.max_keepalive_connections,
                    pool_maxsize=self.settings.max_connections,
                    max_retries=0,
                )
                session.mount("http://", adapter)
                session.mount("https://", adapter)
                self._sessions[key] = session
            return self._sessions[key]

    def _request_kwargs(self, kwargs: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
        headers = kwargs.get("headers")
        if headers is None:
            headers = {"User-Agent": self._get_user_agent(), "Accept": "*/*"}
        elif "User-Agent" not in headers and "user-agent" not in headers:
            headers = {**headers, "User-Agent": self._get_user_agent()}

        built: dict[str, Any] = {
            "headers": headers,
            "cookies": kwargs.get("cookies"),
            "params": kwargs.get("params"),
            "data": kwargs.get("data"),
            "json": kwargs.get("json"),
            "files": kwargs.get("files"),
            "allow_redirects": bool(kwargs.get("allow_redirects", True)),
            "timeout": (self.settings.connect_timeout, kwargs.get("timeout") or self.timeout),
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
        self.reject_impersonate(impersonate)
        method = method.upper()
        if method not in _SUPPORTED_METHODS:
            raise ValidationError(f"Unsupported HTTP method: {method}")

        extra = self.parse_extra_kwargs(kwargs)
        session = self._get_session(proxy, verify)
        request_kwargs = self._request_kwargs(
            {
                "headers": headers,
                "cookies": cookies,
                "params": params,
                "data": data,
                "json": json,
                "files": files,
                "allow_redirects": allow_redirects,
                "timeout": timeout,
            },
            extra,
        )

        try:
            resp = session.request(method, url, **request_kwargs)
            return Response(
                url=str(resp.url),
                status_code=resp.status_code,
                content=resp.content,
                text=resp.text,
                headers=dict(resp.headers),
                raw_response=resp,
            )
        except Exception as e:
            log.warning(f"niquests request failed for {url}: {e}")
            raise

    def _get_async_session(self, proxy: str | None, verify: bool) -> Any:
        import asyncio

        key = (id(asyncio.get_running_loop()), proxy, verify)
        session = self._async_sessions.get(key)
        if session is not None:
            return session
        session = _load_niquests().AsyncSession(
            pool_connections=self.settings.max_keepalive_connections,
            pool_maxsize=self.settings.max_connections,
            retries=0,
        )
        session.verify = verify
        session.trust_env = bool(self.trust_env)
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}
        self._async_sessions[key] = session
        return session

    @override
    @aretry()
    async def adownload(self, url: str, **kwargs: Any) -> Response:
        self.reject_impersonate(kwargs.get("impersonate"))
        method = str(kwargs.get("method", "GET")).upper()
        if method not in _SUPPORTED_METHODS:
            raise ValidationError(f"Unsupported HTTP method: {method}")

        request_kwargs = self._request_kwargs(kwargs, self.parse_extra_kwargs(kwargs.get("kwargs")))
        session = self._get_async_session(kwargs.get("proxy"), bool(kwargs.get("verify", True)))
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
            log.warning(f"niquests async request failed for {url}: {e}")
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

        session = self._get_session(kwargs.get("proxy"), bool(kwargs.get("verify", True)))
        request_kwargs = self._request_kwargs(kwargs, self.parse_extra_kwargs(kwargs.get("kwargs")))

        try:
            resp = session.request(method, url, stream=True, **request_kwargs)
        except Exception as e:
            log.warning(f"niquests stream failed for {url}: {e}")
            yield StreamHeader(url=url, status_code=-1, error=str(e))
            return

        try:
            yield StreamHeader(
                url=str(resp.url),
                status_code=resp.status_code,
                headers=dict(resp.headers),
                content_length=int(resp.headers.get("content-length") or -1),
            )
            yield from resp.iter_content(chunk_size=chunk_size)
        finally:
            resp.close()

    @override
    def close(self) -> None:
        with self._sessions_lock:
            for session in self._sessions.values():
                try:
                    session.close()
                except Exception as e:
                    log.debug(f"关闭 niquests session 失败: {e}")
            self._sessions.clear()


def is_available() -> bool:
    from ipclick.utils import module_probe

    return module_probe.installed(NIQUESTS_MODULE)


def __getattr__(name: str) -> Any:
    if name == "NIQUESTS_AVAILABLE":
        return is_available()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["NIQUESTS_MODULE", "NiquestsAdapter", "is_available"]
