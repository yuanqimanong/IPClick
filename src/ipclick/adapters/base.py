"""下载适配器抽象接口、流事件和浏览器脚本校验辅助函数。"""

from abc import ABC, abstractmethod
import asyncio
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
import functools
from random import randrange
import re
import threading
from typing import Any, cast

from ipclick.adapters.settings import AdapterSettings
from ipclick.dto.response import Response
from ipclick.exceptions import ValidationError
from ipclick.utils.log_util import log


DEFAULT_CHUNK_SIZE = 64 * 1024

UA_POOL_SIZE = 32


@dataclass
class StreamHeader:
    """适配器流的第一条事件，描述响应元数据或建立流时的错误。"""

    url: str
    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    content_length: int = -1


StreamEvent = StreamHeader | bytes


_JS_AUTHOR_ERRORS = ("SyntaxError", "ReferenceError", "TypeError: ")


_JS_FUNCTION_PREFIXES = ("function", "async", "(", "=>")


def normalize_js(script: str) -> str:
    """将表达式或函数体规范化为页面 ``evaluate`` 可执行的函数。"""
    text = script.strip()
    if not text:
        return text
    if text.startswith(_JS_FUNCTION_PREFIXES):
        return text
    if re.search(r"\breturn\b", text):
        return f"() => {{ {text} }}"
    return f"() => ({text})"


_PERMANENT_NAV_ERRORS = (
    "ERR_UNSAFE_PORT",
    "ERR_UNKNOWN_URL_SCHEME",
    "ERR_INVALID_URL",
    "ERR_DISALLOWED_URL_SCHEME",
    "ERR_BLOCKED_BY_CLIENT",
)


def mark_utf8_charset(headers: dict[str, str]) -> dict[str, str]:
    """把 headers 里的 charset 改写成 utf-8，保留原 media type。

    浏览器适配器交出的是 ``page.content()`` 编码后的 UTF-8 字节，而 headers 是从
    原响应原样抄来的。原站点声明 ``charset=gb2312`` 时两者就对不上了——客户端
    ``DownloadResponse.text`` 只按 content-type 里的 charset 解码，于是必然乱码。
    这里只换 charset 参数、不动 media type：xhtml / xml 页面丢了 media type 会影响下游判断。
    """
    for key, value in headers.items():
        if key.lower() != "content-type":
            continue
        stripped = re.sub(r"(?i);\s*charset=[^;]*", "", value).rstrip("; ")
        headers[key] = f"{stripped}; charset=utf-8" if stripped else "text/html; charset=utf-8"
        return headers
    headers["content-type"] = "text/html; charset=utf-8"
    return headers


def raise_if_permanent_navigation_error(error: Exception) -> None:
    """将无需重试的浏览器导航错误转换为参数校验错误。"""
    text = str(error)
    for marker in _PERMANENT_NAV_ERRORS:
        if marker in text:
            raise ValidationError(f"浏览器拒绝访问该 URL（{marker}）：{text}") from error


def raise_if_script_error(error: Exception, script: str | None) -> None:
    """将明确的用户脚本语法/运行错误转换为参数校验错误。"""
    if not script:
        return
    text = str(error)
    if any(name in text for name in _JS_AUTHOR_ERRORS):
        raise ValidationError(
            f"automation_script 有错（它是在页面里执行的 JavaScript，不是 Python）：{text}"
        ) from error


class DownloaderAdapter(ABC):
    """所有本地 HTTP/浏览器下载适配器的统一接口。"""

    adapter_name: str = "base_downloader_adapter"

    def __init__(self, settings: AdapterSettings | None = None):
        """应用通用超时、重试、环境代理和 User-Agent 设置。"""
        self.settings: AdapterSettings = settings or AdapterSettings()
        self._ua_lock: threading.Lock = threading.Lock()
        self._ua_pool_cache: list[str] | None = None

        self.proxy: str | None = None
        self.max_retries: int = self.settings.max_attempts
        self.retry_delay: float = self.settings.initial_backoff
        self.timeout: float = self.settings.download_timeout
        self.connect_timeout: float = self.settings.connect_timeout
        self.verify_ssl: bool = True
        self.trust_env: bool = self.settings.trust_env
        self.user_agent: str = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

    @abstractmethod
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
        allowed_status_codes: list[Any] | None = None,
        kwargs: str | None = None,
    ) -> Response:
        """执行一次逻辑下载；具体传输由子类实现。"""
        raise NotImplementedError

    supports_async: bool = False

    async def adownload(self, url: str, **kwargs: Any) -> Response:
        """在线程池中调用同步实现，供不支持原生异步的适配器兜底。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, functools.partial(self.download, url, **kwargs))

    async def adownload_stream(self, url: str, **kwargs: Any) -> "AsyncIterator[StreamEvent]":
        """在线程池中逐项推进同步流，避免阻塞事件循环。"""
        loop = asyncio.get_running_loop()
        iterator = await loop.run_in_executor(None, functools.partial(self.download_stream, url, **kwargs))
        sentinel = object()
        while True:
            item = await loop.run_in_executor(None, next, iterator, sentinel)
            if item is sentinel:
                return
            yield cast(StreamEvent, item)

    def download_stream(
        self,
        url: str,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        **kwargs: Any,
    ) -> "Iterator[StreamEvent]":
        """把非流式响应按固定大小切块，作为默认流式实现。"""
        response = self.download(url, **kwargs)
        yield StreamHeader(
            url=response.url,
            status_code=response.status_code,
            headers=response.headers or {},
            error=str(response.exception) if response.exception else None,
            content_length=len(response.content) if response.content else 0,
        )
        content = response.content or b""
        for start in range(0, len(content), chunk_size):
            yield content[start : start + chunk_size]

    def reject_impersonate(self, impersonate: str | None) -> None:
        """拒绝当前适配器无法实现的 TLS/浏览器指纹伪装。"""
        if impersonate:
            raise ValidationError(
                f"{self.adapter_name} 不支持浏览器指纹伪装（impersonate={impersonate!r}）。"
                f"需要指纹伪装请用 adapter=curl_cffi，或去掉 impersonate 参数"
            )

    @staticmethod
    def parse_extra_kwargs(raw: str | None) -> dict[str, Any]:
        """解析受控透传参数 JSON；格式错误时记录告警并返回空字典。"""
        if not raw:
            return {}
        try:
            import json as _json

            parsed = _json.loads(raw)
        except (ValueError, TypeError):
            log.warning("kwargs 不是合法 JSON，已忽略")
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _get_user_agent(self) -> str:
        pool = self._ua_pool
        if pool:
            return pool[randrange(len(pool))]
        return self.user_agent

    @property
    def _ua_pool(self) -> list[str]:
        cached = self._ua_pool_cache
        if cached is not None:
            return cached
        with self._ua_lock:
            if self._ua_pool_cache is not None:
                return self._ua_pool_cache
            generator = getattr(self, "ua_generator", None)
            pool: list[str] = []
            if generator is not None:
                for _ in range(UA_POOL_SIZE):
                    try:
                        pool.append(str(generator.random))
                    except Exception:
                        log.debug("fake_useragent 取值失败，使用内置 User-Agent")
                        break
            self._ua_pool_cache = sorted(set(pool)) or [self.user_agent]
            return self._ua_pool_cache

    def get(self, url: str, **kwargs: Any) -> Response:
        """发送 GET 请求。"""
        return self.download(url, method="GET", **kwargs)

    def post(self, url: str, **kwargs: Any) -> Response:
        """发送 POST 请求。"""
        return self.download(url, method="POST", **kwargs)

    def put(self, url: str, **kwargs: Any) -> Response:
        """发送 PUT 请求。"""
        return self.download(url, method="PUT", **kwargs)

    def delete(self, url: str, **kwargs: Any) -> Response:
        """发送 DELETE 请求。"""
        return self.download(url, method="DELETE", **kwargs)

    def head(self, url: str, **kwargs: Any) -> Response:
        """发送 HEAD 请求。"""
        return self.download(url, method="HEAD", **kwargs)

    def options(self, url: str, **kwargs: Any) -> Response:
        """发送 OPTIONS 请求。"""
        return self.download(url, method="OPTIONS", **kwargs)

    def close(self) -> None:
        """释放适配器资源；无状态适配器默认无需处理。"""
        return None

    async def aclose(self) -> None:
        """异步释放适配器资源，默认委托同步关闭。"""
        self.close()

    def __enter__(self) -> "DownloaderAdapter":
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()
