"""适配器级同步/异步指数退避重试策略与装饰器。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
import functools
import math
from random import uniform
import time
from typing import Any, Protocol, final

from ipclick.adapters.settings import DEFAULT_RETRY_STATUS_CODES, HARD_MAX_RETRIES, AdapterSettings
from ipclick.dto.response import Response
from ipclick.exceptions import AdapterError, ValidationError
from ipclick.trace import get_recorder
from ipclick.utils.log_util import log


DEFAULT_MAX_RETRIES = 3

DEFAULT_BASE_DELAY = 1.0

JITTER_RANGE = (0.8, 1.2)

# 由服务层在执行下载前绑定：返回 True 表示调用方还在等这个响应。
# 用 ContextVar 而不是给适配器加属性——适配器实例是进程内共享的，
# 而"调用方还在不在"是每个请求各自的事。
caller_alive_check: ContextVar[Callable[[], bool] | None] = ContextVar("caller_alive_check", default=None)


_UNKNOWN_URL = "unknown"

_UNKNOWN_ADAPTER = "unknown"


class RetryHost(Protocol):
    """重试装饰器从适配器宿主读取的最小属性集合。"""

    adapter_name: str
    settings: AdapterSettings
    max_retries: int
    retry_delay: float


def _delay_from(value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        if isinstance(value, (tuple, list)):
            if len(value) != 2:
                return default
            delay = uniform(float(value[0]), float(value[1]))
        else:
            delay = float(value)
        return delay if math.isfinite(delay) and delay >= 0 else default
    except (TypeError, ValueError):
        return default


@final
@dataclass(frozen=True)
class RetryPolicy:
    """一次调用解析后的不可变重试策略。"""

    max_retries: int = DEFAULT_MAX_RETRIES
    base_delay: float = DEFAULT_BASE_DELAY
    exponent: float = 2.0
    max_backoff: float = AdapterSettings().max_backoff
    retry_codes: frozenset[int] = DEFAULT_RETRY_STATUS_CODES
    allowed_status_codes: frozenset[int] = frozenset()

    @classmethod
    def resolve(cls, host: object, call_kwargs: dict[str, Any]) -> RetryPolicy:
        """合并调用参数、适配器属性和全局设置。"""
        settings: AdapterSettings | None = getattr(host, "settings", None)
        defaults = cls()

        requested_retries = call_kwargs.get("max_retries")
        if requested_retries is None:
            requested_retries = getattr(host, "max_retries", defaults.max_retries)
        try:
            # 直接调用适配器也可能绕过 DTO 和 RPC 校验，这里保留最后一道
            # 防御性硬上限，避免构造近乎无限的重试循环。
            max_retries = min(max(0, int(requested_retries)), HARD_MAX_RETRIES)
        except (TypeError, ValueError, OverflowError):
            max_retries = defaults.max_retries

        requested_delay = call_kwargs.get("retry_delay")
        if requested_delay is None:
            requested_delay = getattr(host, "retry_delay", defaults.base_delay)

        allowed = call_kwargs.get("allowed_status_codes") or ()

        return cls(
            max_retries=max_retries,
            base_delay=_delay_from(requested_delay, defaults.base_delay),
            exponent=settings.backoff_exponent if settings else defaults.exponent,
            max_backoff=settings.max_backoff if settings else defaults.max_backoff,
            retry_codes=settings.retry_codes if settings else defaults.retry_codes,
            allowed_status_codes=frozenset(int(code) for code in allowed),
        )

    @property
    def total_attempts(self) -> int:
        """返回首次调用加重试次数。"""
        return self.max_retries + 1

    def retries_status(self, status: int | None) -> bool:
        """判断 HTTP 状态是否需要重试且未被调用方显式允许。"""
        if not isinstance(status, int):
            return False
        return status in self.retry_codes and status not in self.allowed_status_codes

    def delay_for(self, attempt: int) -> float:
        """计算带随机抖动且受上限约束的退避时间。"""
        capped = min(self.base_delay * (self.exponent**attempt), self.max_backoff)
        return capped * uniform(*JITTER_RANGE)


def _never_succeeds(error: Exception) -> bool:
    """这个异常再试多少次结果都一样吗？

    重试解决的是"目标站点抖了"。参数根本不合法时重试只是把失败等待按次数放大：
    实测 `--impersonate nosuchbrowser999` 在默认 3 次重试下要 14 秒才报错，
    而 `data=12345` 这种类型错同样白等四轮——两者都不可能因为再试一次而成立。
    """
    if isinstance(error, (TypeError, ValueError)):
        return True
    try:
        from curl_cffi.requests.exceptions import ImpersonateError
    except ImportError:
        return False
    return isinstance(error, ImpersonateError)


@final
class RetryLoop:
    """记录单次逻辑请求的尝试次数、最后错误和追踪事件。"""

    def __init__(self, policy: RetryPolicy, url: str, adapter_name: str) -> None:
        """绑定策略及用于日志和追踪的请求身份。"""
        self.policy: RetryPolicy = policy
        self.url: str = url
        self.adapter_name: str = adapter_name
        self.last_error: Exception | None = None

    def attempts(self) -> Iterator[int]:
        """依次产出从零开始的尝试序号。"""
        return iter(range(self.policy.total_attempts))

    @staticmethod
    def stamp(result: Response, attempt: int, started: float) -> Response:
        """补齐响应耗时和实际尝试次数。"""
        if result.elapsed_ms == 0:
            result.elapsed_ms = int((time.monotonic() - started) * 1000)
        result.attempts = attempt + 1
        return result

    def on_result(self, attempt: int, result: Response) -> float | None:
        """处理可重试状态码，返回等待时间或 ``None``。"""
        if attempt >= self.policy.max_retries or not self.policy.retries_status(result.status_code):
            return None
        if caller_gone():
            log.info(f"调用方已不再等待 {self.url}，放弃剩余重试")
            return None
        delay = self.policy.delay_for(attempt)
        get_recorder().record_retry(self.adapter_name, "status_code")
        log.warning(
            f"Download {self.url} returned {result.status_code}, "
            f"retrying {attempt + 1}/{self.policy.max_retries} in {delay:.1f}s..."
        )
        return delay

    def on_error(self, attempt: int, error: Exception) -> float | None:
        """记录传输异常，返回等待时间或 ``None``。"""
        self.last_error = error
        if attempt >= self.policy.max_retries:
            return None
        if _never_succeeds(error):
            log.warning(f"Download {self.url} failed with a permanent error, not retrying: {error}")
            return None
        if caller_gone():
            # 重试把耗时按尝试次数放大：max_retries 上限是 20，配上退避总和最坏能拖到
            # 八分钟以上。调用方的 deadline 早就过了还在对目标站点重投，纯粹是把请求
            # 放大打到别人身上，而且占着线程池不放。
            log.info(f"调用方已不再等待 {self.url}，放弃剩余重试（最后错误：{error}）")
            return None
        delay = self.policy.delay_for(attempt)
        get_recorder().record_retry(self.adapter_name, "exception")
        log.warning(
            f"Download {self.url} failed, retrying {attempt + 1}/{self.policy.max_retries} "
            f"in {delay:.1f}s... Error: {error}"
        )
        return delay

    def give_up(self, attempt: int) -> Response:
        """把最后异常转换成稳定的错误响应。"""
        error = self.last_error or Exception("Max retries exceeded")
        return Response.error_response(self.url, error, attempts=attempt + 1)


def caller_gone() -> bool:
    """当前请求的调用方是否已经不再等待。"""
    predicate = caller_alive_check.get()
    if predicate is None:
        return False
    try:
        return not predicate()
    except Exception:
        # 判定本身出错不该影响重试决策，按"还在等"处理。
        return False


def _loop_for(host: object, args: tuple[Any, ...], kwargs: dict[str, Any]) -> RetryLoop:
    url = str(args[0]) if args else str(kwargs.get("url", _UNKNOWN_URL))
    adapter_name = str(getattr(host, "adapter_name", _UNKNOWN_ADAPTER))
    return RetryLoop(RetryPolicy.resolve(host, kwargs), url, adapter_name)


def retry() -> Callable[[Callable[..., Response]], Callable[..., Response]]:
    """装饰同步适配器方法，使异常和指定状态码按策略重试。"""

    def decorator(func: Callable[..., Response]) -> Callable[..., Response]:
        @functools.wraps(func)
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Response:
            loop = _loop_for(self, args, kwargs)
            attempt = 0
            for attempt in loop.attempts():
                started = time.monotonic()
                try:
                    result = loop.stamp(func(self, *args, **kwargs), attempt, started)
                except (ValidationError, AdapterError):
                    raise
                except Exception as e:
                    delay = loop.on_error(attempt, e)
                    if delay is None:
                        return loop.give_up(attempt)
                    time.sleep(delay)
                    continue

                delay = loop.on_result(attempt, result)
                if delay is None:
                    return result
                time.sleep(delay)
            return loop.give_up(attempt)

        return wrapper

    return decorator


def aretry() -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """异步版本的适配器重试装饰器。"""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Response:
            loop = _loop_for(self, args, kwargs)
            attempt = 0
            for attempt in loop.attempts():
                started = time.monotonic()
                try:
                    result = loop.stamp(await func(self, *args, **kwargs), attempt, started)
                except (ValidationError, AdapterError):
                    raise
                except Exception as e:
                    delay = loop.on_error(attempt, e)
                    if delay is None:
                        return loop.give_up(attempt)
                    await asyncio.sleep(delay)
                    continue

                delay = loop.on_result(attempt, result)
                if delay is None:
                    return result
                await asyncio.sleep(delay)
            return loop.give_up(attempt)

        return wrapper

    return decorator


__all__ = ["JITTER_RANGE", "RetryHost", "RetryLoop", "RetryPolicy", "aretry", "retry"]
