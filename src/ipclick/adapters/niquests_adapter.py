"""niquests 适配器。

``niquests`` 是 ``requests`` 的 drop-in 替代：API 完全一致，但底层换成
``urllib3-future``，因而支持 **HTTP/2 与 HTTP/3**，而 requests 停在 HTTP/1.1。
既然接口一样、能力更强，本项目直接用它替掉了原来的 niquests 适配器
（``requests`` 适配器已移除，见 CHANGELOG）。

可选依赖（``pip install "ipclick[niquests]"``）：它不像 curl_cffi 那样有浏览器
指纹伪装，主要价值是 HTTP/3 和"用起来就是 requests"这份熟悉感。
"""

from __future__ import annotations

from collections.abc import Iterator
import threading
from typing import Any

from typing_extensions import override

from ipclick.adapters.base import DEFAULT_CHUNK_SIZE, DownloaderAdapter, StreamEvent, StreamHeader, retry
from ipclick.adapters.settings import AdapterSettings
from ipclick.dto.response import Response
from ipclick.exceptions import AdapterError, ValidationError
from ipclick.utils.log_util import log


# 可选依赖：缺失时降级为 None，由 __init__ 抛 AdapterError
_niquests: Any
_user_agent_cls: Any

try:
    import niquests as _niquests
except ImportError:  # pragma: no cover - 取决于安装环境
    _niquests = None

try:
    from fake_useragent import UserAgent as _user_agent_cls
except ImportError:  # pragma: no cover - 取决于安装环境
    _user_agent_cls = None

NIQUESTS_AVAILABLE: bool = _niquests is not None

_SUPPORTED_METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"})

# 允许调用方通过 kwargs 透传的参数
_PASSTHROUGH_KWARGS = frozenset({"auth", "cert", "hooks"})


class NiquestsAdapter(DownloaderAdapter):
    """基于 ``niquests`` 的适配器。

    相比 curl_cffi 的取舍：
    - 没有浏览器指纹伪装（``impersonate`` 参数会被忽略）
    - 支持 HTTP/2 与 HTTP/3（本项目里唯一支持 HTTP/3 的适配器）
    - API 与 requests 完全一致，适合对接已有 requests 代码
    """

    adapter_name: str = "niquests"

    def __init__(self, settings: AdapterSettings | None = None):
        if _niquests is None:
            raise AdapterError('niquests is not installed. Install it with: pip install "ipclick[niquests]"')

        super().__init__(settings)
        # 按 (proxy, verify) 缓存 Session，以复用连接池
        self._sessions: dict[tuple[str | None, bool], Any] = {}
        self._sessions_lock: threading.Lock = threading.Lock()
        self.ua_generator: Any = _user_agent_cls(platforms="desktop") if _user_agent_cls is not None else None

    def _get_session(self, proxy: str | None, verify: bool) -> Any:
        key = (proxy, verify)
        session = self._sessions.get(key)
        if session is not None:
            return session

        with self._sessions_lock:
            if key not in self._sessions:
                session = _niquests.Session()
                session.verify = verify
                # 与另外两个适配器保持一致：默认不继承环境里的 HTTP_PROXY，
                # 代理必须由调用方显式指定。
                session.trust_env = bool(self.trust_env)
                if proxy:
                    session.proxies = {"http": proxy, "https": proxy}
                adapter = _niquests.adapters.HTTPAdapter(
                    pool_connections=self.settings.max_keepalive_connections,
                    pool_maxsize=self.settings.max_connections,
                    max_retries=0,  # 重试由本项目的 retry 装饰器统一负责
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
            # niquests（同 requests）的 timeout 传单值时同时作用于连接与读取；
            # 拆成 (连接, 读取) 才能让 [DOWNLOADER].connect_timeout 真正生效。
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
        """使用 niquests 执行 HTTP 请求。"""
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
            # 只记一行，堆栈交给 retry 装饰器最终失败时处理
            log.warning(f"niquests request failed for {url}: {e}")
            raise

    @override
    def download_stream(
        self,
        url: str,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        **kwargs: Any,
    ) -> Iterator[StreamEvent]:
        """niquests 的真流式实现。"""
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
        """关闭所有缓存的 Session"""
        with self._sessions_lock:
            for session in self._sessions.values():
                try:
                    session.close()
                except Exception as e:
                    log.debug(f"关闭 niquests session 失败: {e}")
            self._sessions.clear()


def is_available() -> bool:
    """检查 niquests 是否可用"""
    return NIQUESTS_AVAILABLE


__all__ = ["NIQUESTS_AVAILABLE", "NiquestsAdapter", "is_available"]
