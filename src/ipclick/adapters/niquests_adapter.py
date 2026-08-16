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


# 可选依赖：**懒加载**，缺失时降级为 None，由 __init__ 抛 AdapterError。
#
# 为什么不在模块级 import：那会把"装没装"的结论固化在进程启动那一刻，
# 运行时 pip install niquests 之后不重启进程就永远看不到它
# （详见 :mod:`ipclick.utils.module_probe`）。
_UNPROBED: Any = object()
_niquests: Any = _UNPROBED
_user_agent_cls: Any = _UNPROBED

#: 探测用的顶层模块名
NIQUESTS_MODULE = "niquests"


def _load_niquests() -> Any:
    global _niquests
    if _niquests is _UNPROBED:
        try:
            import niquests

            _niquests = niquests
        except ImportError:  # pragma: no cover - 取决于安装环境
            _niquests = None
    return _niquests


def _load_user_agent() -> Any:
    global _user_agent_cls
    if _user_agent_cls is _UNPROBED:
        try:
            from fake_useragent import UserAgent

            _user_agent_cls = UserAgent
        except ImportError:  # pragma: no cover - 取决于安装环境
            _user_agent_cls = None
    return _user_agent_cls


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
        # 真正的 import 就发生在这里——"能不能用"的展示层走 find_spec，
        # 执行路径仍然老老实实 import 一次。
        if _load_niquests() is None:
            raise AdapterError('niquests is not installed. Install it with: pip install "ipclick[niquests]"')

        super().__init__(settings)
        # 按 (proxy, verify) 缓存 Session，以复用连接池
        self._sessions: dict[tuple[str | None, bool], Any] = {}
        self._sessions_lock: threading.Lock = threading.Lock()
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
                # 与另外两个适配器保持一致：默认不继承环境里的 HTTP_PROXY，
                # 代理必须由调用方显式指定。
                session.trust_env = bool(self.trust_env)
                if proxy:
                    session.proxies = {"http": proxy, "https": proxy}
                adapter = _load_niquests().adapters.HTTPAdapter(
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
    """niquests 装了没。

    走 find_spec 而不是 ``_niquests is not None``：后者只有在有人真的构造过一次
    适配器之后才有意义，而这个函数恰恰是在那之前被问的。
    """
    from ipclick.utils import module_probe

    return module_probe.installed(NIQUESTS_MODULE)


def __getattr__(name: str) -> Any:
    """``NIQUESTS_AVAILABLE`` 的兼容层。

    0.3 里它是模块级常量，值在 import 那一刻就定死了。0.4 起改成每次问都重新
    探测——运行时装完 niquests 不重启也能用。保留这个名字是为了不破坏
    ``from ... import NIQUESTS_AVAILABLE`` 的写法，但那种写法拿到的仍是一个快照，
    新代码请直接调 :func:`is_available`。
    """
    if name == "NIQUESTS_AVAILABLE":
        return is_available()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["NIQUESTS_MODULE", "NiquestsAdapter", "is_available"]
