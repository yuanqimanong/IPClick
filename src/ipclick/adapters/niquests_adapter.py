"""基于 niquests 的 HTTP/2、HTTP/3 同步和异步适配器。"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from typing_extensions import override

from ipclick.adapters.base import DEFAULT_CHUNK_SIZE, DownloaderAdapter, StreamEvent, StreamHeader
from ipclick.adapters.redirects import afollow_with_policy, follow_with_policy
from ipclick.adapters.retry import aretry, retry
from ipclick.adapters.sessions import AsyncSessionCache, SessionCache, reset_cookies
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

SessionKey = tuple[str | None, bool]


class NiquestsAdapter(DownloaderAdapter):
    """按代理和 TLS 校验设置复用 niquests 连接池。"""

    adapter_name: str = "niquests"
    supports_async: bool = True

    def __init__(self, settings: AdapterSettings | None = None):
        """按需加载依赖并初始化同步、异步 session 缓存。"""
        if _load_niquests() is None:
            raise AdapterError('niquests is not installed. Install it with: pip install "ipclick[niquests]"')

        super().__init__(settings)
        self._sessions: SessionCache[SessionKey] = SessionCache(self.adapter_name, self._new_session)
        self._async_sessions: AsyncSessionCache[SessionKey] = AsyncSessionCache(
            self.adapter_name, self._new_async_session
        )
        user_agent = _load_user_agent()
        self.ua_generator: Any = user_agent(platforms="desktop") if user_agent is not None else None

    def _apply_common(self, session: Any, key: SessionKey) -> Any:
        proxy, verify = key
        session.verify = verify
        session.trust_env = bool(self.trust_env)
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}
        return session

    def _new_session(self, key: SessionKey) -> Any:
        session = self._apply_common(_load_niquests().Session(), key)
        adapter = _load_niquests().adapters.HTTPAdapter(
            pool_connections=self.settings.max_keepalive_connections,
            pool_maxsize=self.settings.max_connections,
            max_retries=0,
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def _new_async_session(self, key: SessionKey) -> Any:
        return self._apply_common(
            _load_niquests().AsyncSession(
                pool_connections=self.settings.max_keepalive_connections,
                pool_maxsize=self.settings.max_connections,
                retries=0,
            ),
            key,
        )

    def _request_kwargs(self, kwargs: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
        # 三个入口（download / adownload / download_stream）都经过这里。
        self.reject_browser_only_params(kwargs.get("automation_config"), kwargs.get("automation_script"))
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

    def _request_following_policy(
        self,
        session: Any,
        method: str,
        url: str,
        request_kwargs: dict[str, Any],
        allow_redirects: bool,
    ) -> Any:
        """发一次请求；装了 url_validator 时逐跳跟随重定向并逐跳校验。

        理由同 curl_cffi 适配器：SSRF 准入只校验入口 URL，让底层库自行跟随重定向的话，
        一次 302 就能跳到云元数据或内网地址而完全绕过 [SECURITY] 策略。
        """
        validator = self.url_validator
        if validator is None or not allow_redirects:
            return session.request(method, url, **request_kwargs)

        prepare, saved_body = self._hop_sender(request_kwargs)

        def send(hop_url: str, hop_method: str, body: Any) -> Any:
            return session.request(hop_method, hop_url, **prepare(body))

        return follow_with_policy(send, url, method, saved_body, validator)

    def _hop_sender(self, request_kwargs: dict[str, Any], *, stream: bool = False) -> tuple[Any, Any]:
        """构造逐跳请求所需的 kwargs 与初始请求体快照（理由同 curl_cffi 适配器）。"""
        hop_kwargs = dict(request_kwargs)
        hop_kwargs["allow_redirects"] = False
        if stream:
            hop_kwargs["stream"] = True
        body_keys = ("data", "json", "files")
        saved_body = {key: hop_kwargs.get(key) for key in body_keys}

        def prepare(body: Any) -> dict[str, Any]:
            kw = dict(hop_kwargs)
            if body is None:
                for key in body_keys:
                    _ = kw.pop(key, None)
            return kw

        return prepare, saved_body

    async def _arequest_following_policy(
        self,
        session: Any,
        method: str,
        url: str,
        request_kwargs: dict[str, Any],
        allow_redirects: bool,
    ) -> Any:
        """异步版的逐跳跟随；原来这条路完全不过准入（见 curl_cffi 适配器同名方法）。"""
        validator = self.url_validator
        if validator is None or not allow_redirects:
            return await session.request(method, url, **request_kwargs)

        prepare, saved_body = self._hop_sender(request_kwargs)

        async def send(hop_url: str, hop_method: str, body: Any) -> Any:
            return await session.request(hop_method, hop_url, **prepare(body))

        return await afollow_with_policy(send, url, method, saved_body, validator)

    def _stream_following_policy(
        self,
        session: Any,
        method: str,
        url: str,
        request_kwargs: dict[str, Any],
        allow_redirects: bool,
    ) -> Any:
        """流式版的逐跳跟随，返回最后一跳那条流（见 curl_cffi 适配器同名方法）。"""
        validator = self.url_validator
        if validator is None or not allow_redirects:
            return session.request(method, url, stream=True, **request_kwargs)

        prepare, saved_body = self._hop_sender(request_kwargs, stream=True)

        def send(hop_url: str, hop_method: str, body: Any) -> Any:
            return session.request(hop_method, hop_url, **prepare(body))

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
        self.reject_impersonate(impersonate)
        method = method.upper()
        if method not in _SUPPORTED_METHODS:
            raise ValidationError(f"Unsupported HTTP method: {method}")

        extra = self.parse_extra_kwargs(kwargs)
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
                "automation_config": automation_config,
                "automation_script": automation_script,
            },
            extra,
        )

        # 用 lease 而不是 get：整个请求期间都持着这个 session，而 LRU 会在别的线程
        # 把它淘汰并 close 掉（见 sessions._SessionEntry）。
        with self._sessions.lease((proxy, verify)) as session:
            reset_cookies(session)
            try:
                resp = self._request_following_policy(session, method, url, request_kwargs, allow_redirects)
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

    @override
    @aretry()
    async def adownload(self, url: str, **kwargs: Any) -> Response:
        """使用 niquests AsyncSession 异步发送请求。"""
        self.reject_impersonate(kwargs.get("impersonate"))
        method = str(kwargs.get("method", "GET")).upper()
        if method not in _SUPPORTED_METHODS:
            raise ValidationError(f"Unsupported HTTP method: {method}")

        request_kwargs = self._request_kwargs(kwargs, self.parse_extra_kwargs(kwargs.get("kwargs")))
        with self._async_sessions.lease((kwargs.get("proxy"), bool(kwargs.get("verify", True)))) as session:
            reset_cookies(session)
            try:
                resp = await self._arequest_following_policy(
                    session, method, url, request_kwargs, bool(request_kwargs.get("allow_redirects", True))
                )
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
        """产出响应首部和正文分片，并确保响应最终关闭。"""
        self.reject_impersonate(kwargs.get("impersonate"))
        method = str(kwargs.get("method", "GET")).upper()
        if method not in _SUPPORTED_METHODS:
            yield StreamHeader(url=url, status_code=-1, error=f"Unsupported HTTP method: {method}")
            return

        # 租借必须覆盖**整条流**的生命周期：流式请求持有 session 的时间最长，而它的
        # "最近使用时间"停在开流那一刻，最容易先变成 LRU 被淘汰关掉。
        with self._sessions.lease((kwargs.get("proxy"), bool(kwargs.get("verify", True)))) as session:
            reset_cookies(session)
            request_kwargs = self._request_kwargs(kwargs, self.parse_extra_kwargs(kwargs.get("kwargs")))

            try:
                resp = self._stream_following_policy(
                    session, method, url, request_kwargs, bool(request_kwargs.get("allow_redirects", True))
                )
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
                # 即使调用方提前停止迭代，也要归还连接池资源。
                resp.close()

    @override
    def close(self) -> None:
        """关闭同步缓存，并调度关闭异步 session。"""
        self._sessions.close()
        self._async_sessions.close()

    @override
    async def aclose(self) -> None:
        """在正确事件循环中关闭全部 session。"""
        self._sessions.close()
        await self._async_sessions.aclose()


def is_available() -> bool:
    """返回 niquests 模块当前是否可导入。"""
    from ipclick.utils import module_probe

    return module_probe.installed(NIQUESTS_MODULE)


def __getattr__(name: str) -> Any:
    if name == "NIQUESTS_AVAILABLE":
        return is_available()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["NIQUESTS_MODULE", "NiquestsAdapter", "is_available"]
