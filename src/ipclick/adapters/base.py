from abc import ABC, abstractmethod
import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
import functools
from random import randrange, uniform
import re
import threading
import time
from typing import Any, cast

from ipclick.adapters.settings import DEFAULT_RETRY_STATUS_CODES, AdapterSettings
from ipclick.dto.response import Response
from ipclick.exceptions import AdapterError, ValidationError
from ipclick.trace import get_recorder
from ipclick.utils.log_util import log


DEFAULT_CHUNK_SIZE = 64 * 1024

UA_POOL_SIZE = 32


@dataclass
class StreamHeader:
    url: str
    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    content_length: int = -1


StreamEvent = StreamHeader | bytes


MAX_RETRY_DELAY = AdapterSettings().max_backoff


def _coerce_delay(value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        if isinstance(value, (tuple, list)):
            if len(value) != 2:
                return default
            low, high = float(value[0]), float(value[1])
            return uniform(low, high)
        return float(value)
    except (TypeError, ValueError):
        return default


def retry(
    max_retries_attr: str = "max_retries", retry_delay_attr: str = "retry_delay"
) -> Callable[[Callable[..., Response]], Callable[..., Response]]:

    def decorator(func: Callable[..., Response]) -> Callable[..., Response]:
        @functools.wraps(func)
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Response:
            requested_retries = kwargs.get("max_retries")
            max_retries = (
                int(requested_retries) if requested_retries is not None else getattr(self, max_retries_attr, 3)
            )
            max_retries = max(0, int(max_retries))

            requested_delay = kwargs.get("retry_delay")
            base_delay = _coerce_delay(
                requested_delay if requested_delay is not None else getattr(self, retry_delay_attr, 1.0),
                default=1.0,
            )

            url = args[0] if args else kwargs.get("url", "unknown")
            allowed = kwargs.get("allowed_status_codes") or None

            settings: AdapterSettings | None = getattr(self, "settings", None)
            retry_codes = settings.retry_codes if settings else DEFAULT_RETRY_STATUS_CODES
            exponent = settings.backoff_exponent if settings else 2.0
            max_backoff = settings.max_backoff if settings else MAX_RETRY_DELAY

            last_exception: Exception | None = None

            for attempt in range(max_retries + 1):
                start_time = time.monotonic()
                try:
                    result = func(self, *args, **kwargs)

                    if hasattr(result, "elapsed_ms") and result.elapsed_ms == 0:
                        result.elapsed_ms = int((time.monotonic() - start_time) * 1000)

                    if hasattr(result, "attempts"):
                        result.attempts = attempt + 1

                    status = getattr(result, "status_code", None)
                    if (
                        attempt < max_retries
                        and isinstance(status, int)
                        and status in retry_codes
                        and not (allowed and status in allowed)
                    ):
                        sleep_time = _backoff(attempt, base_delay, exponent, max_backoff)
                        get_recorder().record_retry(getattr(self, "adapter_name", "unknown"), "status_code")
                        log.warning(
                            f"Download {url} returned {status}, "
                            f"retrying {attempt + 1}/{max_retries} in {sleep_time:.1f}s..."
                        )
                        time.sleep(sleep_time)
                        continue

                    return result

                except ValidationError:
                    raise

                except AdapterError:
                    raise

                except Exception as e:
                    last_exception = e

                    if attempt >= max_retries:
                        return Response.error_response(url, e, attempts=attempt + 1)

                    sleep_time = _backoff(attempt, base_delay, exponent, max_backoff)
                    get_recorder().record_retry(getattr(self, "adapter_name", "unknown"), "exception")
                    log.warning(
                        f"Download {url} failed, retrying {attempt + 1}/{max_retries} "
                        f"in {sleep_time:.1f}s... Error: {e}"
                    )
                    time.sleep(sleep_time)

            return Response.error_response(
                url, last_exception or Exception("Max retries exceeded"), attempts=max_retries + 1
            )

        return wrapper

    return decorator


def aretry(
    max_retries_attr: str = "max_retries", retry_delay_attr: str = "retry_delay"
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Response:
            requested_retries = kwargs.get("max_retries")
            max_retries = max(
                0, int(requested_retries) if requested_retries is not None else getattr(self, max_retries_attr, 3)
            )
            requested_delay = kwargs.get("retry_delay")
            base_delay = _coerce_delay(
                requested_delay if requested_delay is not None else getattr(self, retry_delay_attr, 1.0),
                default=1.0,
            )
            url = args[0] if args else kwargs.get("url", "unknown")
            allowed = kwargs.get("allowed_status_codes") or None

            settings: AdapterSettings | None = getattr(self, "settings", None)
            retry_codes = settings.retry_codes if settings else DEFAULT_RETRY_STATUS_CODES
            exponent = settings.backoff_exponent if settings else 2.0
            max_backoff = settings.max_backoff if settings else MAX_RETRY_DELAY

            last_exception: Exception | None = None
            for attempt in range(max_retries + 1):
                start_time = time.monotonic()
                try:
                    result = await func(self, *args, **kwargs)
                    if hasattr(result, "elapsed_ms") and result.elapsed_ms == 0:
                        result.elapsed_ms = int((time.monotonic() - start_time) * 1000)
                    if hasattr(result, "attempts"):
                        result.attempts = attempt + 1

                    status = getattr(result, "status_code", None)
                    if (
                        attempt < max_retries
                        and isinstance(status, int)
                        and status in retry_codes
                        and not (allowed and status in allowed)
                    ):
                        sleep_time = _backoff(attempt, base_delay, exponent, max_backoff)
                        get_recorder().record_retry(getattr(self, "adapter_name", "unknown"), "status_code")
                        log.warning(
                            f"Download {url} returned {status}, retrying {attempt + 1}/{max_retries} "
                            f"in {sleep_time:.1f}s..."
                        )
                        await asyncio.sleep(sleep_time)
                        continue
                    return result

                except (ValidationError, AdapterError):
                    raise
                except Exception as e:
                    last_exception = e
                    if attempt >= max_retries:
                        return Response.error_response(url, e, attempts=attempt + 1)
                    sleep_time = _backoff(attempt, base_delay, exponent, max_backoff)
                    get_recorder().record_retry(getattr(self, "adapter_name", "unknown"), "exception")
                    log.warning(
                        f"Download {url} failed, retrying {attempt + 1}/{max_retries} "
                        f"in {sleep_time:.1f}s... Error: {e}"
                    )
                    await asyncio.sleep(sleep_time)

            return Response.error_response(
                url, last_exception or Exception("Max retries exceeded"), attempts=max_retries + 1
            )

        return wrapper

    return decorator


def _backoff(
    attempt: int,
    base_delay: float,
    exponent: float = 2.0,
    max_backoff: float = MAX_RETRY_DELAY,
) -> float:
    delay = min(base_delay * (exponent**attempt), max_backoff)
    return delay * uniform(0.8, 1.2)


_JS_AUTHOR_ERRORS = ("SyntaxError", "ReferenceError", "TypeError: ")


_JS_FUNCTION_PREFIXES = ("function", "async", "(", "=>")


def normalize_js(script: str) -> str:
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


def raise_if_permanent_navigation_error(error: Exception) -> None:
    text = str(error)
    for marker in _PERMANENT_NAV_ERRORS:
        if marker in text:
            raise ValidationError(f"浏览器拒绝访问该 URL（{marker}）：{text}") from error


def raise_if_script_error(error: Exception, script: str | None) -> None:
    if not script:
        return
    text = str(error)
    if any(name in text for name in _JS_AUTHOR_ERRORS):
        raise ValidationError(
            f"automation_script 有错（它是在页面里执行的 JavaScript，不是 Python）：{text}"
        ) from error


class DownloaderAdapter(ABC):
    adapter_name: str = "base_downloader_adapter"

    def __init__(self, settings: AdapterSettings | None = None):
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
        raise NotImplementedError

    supports_async: bool = False

    async def adownload(self, url: str, **kwargs: Any) -> Response:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, functools.partial(self.download, url, **kwargs))

    async def adownload_stream(self, url: str, **kwargs: Any) -> "AsyncIterator[StreamEvent]":
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
        if impersonate:
            raise ValidationError(
                f"{self.adapter_name} 不支持浏览器指纹伪装（impersonate={impersonate!r}）。"
                f"需要指纹伪装请用 adapter=curl_cffi，或去掉 impersonate 参数"
            )

    @staticmethod
    def parse_extra_kwargs(raw: str | None) -> dict[str, Any]:
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
        return self.download(url, method="GET", **kwargs)

    def post(self, url: str, **kwargs: Any) -> Response:
        return self.download(url, method="POST", **kwargs)

    def put(self, url: str, **kwargs: Any) -> Response:
        return self.download(url, method="PUT", **kwargs)

    def delete(self, url: str, **kwargs: Any) -> Response:
        return self.download(url, method="DELETE", **kwargs)

    def head(self, url: str, **kwargs: Any) -> Response:
        return self.download(url, method="HEAD", **kwargs)

    def options(self, url: str, **kwargs: Any) -> Response:
        return self.download(url, method="OPTIONS", **kwargs)

    def close(self) -> None:
        return None

    def __enter__(self) -> "DownloaderAdapter":
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()
