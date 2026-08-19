from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from dataclasses import dataclass
import functools
from random import uniform
import time
from typing import Any, Protocol, final

from ipclick.adapters.settings import DEFAULT_RETRY_STATUS_CODES, AdapterSettings
from ipclick.dto.response import Response
from ipclick.exceptions import AdapterError, ValidationError
from ipclick.trace import get_recorder
from ipclick.utils.log_util import log


DEFAULT_MAX_RETRIES = 3

DEFAULT_BASE_DELAY = 1.0

JITTER_RANGE = (0.8, 1.2)

_UNKNOWN_URL = "unknown"

_UNKNOWN_ADAPTER = "unknown"


class RetryHost(Protocol):
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
            return uniform(float(value[0]), float(value[1]))
        return float(value)
    except (TypeError, ValueError):
        return default


@final
@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = DEFAULT_MAX_RETRIES
    base_delay: float = DEFAULT_BASE_DELAY
    exponent: float = 2.0
    max_backoff: float = AdapterSettings().max_backoff
    retry_codes: frozenset[int] = DEFAULT_RETRY_STATUS_CODES
    allowed_status_codes: frozenset[int] = frozenset()

    @classmethod
    def resolve(cls, host: object, call_kwargs: dict[str, Any]) -> RetryPolicy:
        settings: AdapterSettings | None = getattr(host, "settings", None)
        defaults = cls()

        requested_retries = call_kwargs.get("max_retries")
        if requested_retries is None:
            requested_retries = getattr(host, "max_retries", defaults.max_retries)
        try:
            max_retries = max(0, int(requested_retries))
        except (TypeError, ValueError):
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
        return self.max_retries + 1

    def retries_status(self, status: int | None) -> bool:
        if not isinstance(status, int):
            return False
        return status in self.retry_codes and status not in self.allowed_status_codes

    def delay_for(self, attempt: int) -> float:
        capped = min(self.base_delay * (self.exponent**attempt), self.max_backoff)
        return capped * uniform(*JITTER_RANGE)


@final
class RetryLoop:
    def __init__(self, policy: RetryPolicy, url: str, adapter_name: str) -> None:
        self.policy: RetryPolicy = policy
        self.url: str = url
        self.adapter_name: str = adapter_name
        self.last_error: Exception | None = None

    def attempts(self) -> Iterator[int]:
        return iter(range(self.policy.total_attempts))

    @staticmethod
    def stamp(result: Response, attempt: int, started: float) -> Response:
        if result.elapsed_ms == 0:
            result.elapsed_ms = int((time.monotonic() - started) * 1000)
        result.attempts = attempt + 1
        return result

    def on_result(self, attempt: int, result: Response) -> float | None:
        if attempt >= self.policy.max_retries or not self.policy.retries_status(result.status_code):
            return None
        delay = self.policy.delay_for(attempt)
        get_recorder().record_retry(self.adapter_name, "status_code")
        log.warning(
            f"Download {self.url} returned {result.status_code}, "
            f"retrying {attempt + 1}/{self.policy.max_retries} in {delay:.1f}s..."
        )
        return delay

    def on_error(self, attempt: int, error: Exception) -> float | None:
        self.last_error = error
        if attempt >= self.policy.max_retries:
            return None
        delay = self.policy.delay_for(attempt)
        get_recorder().record_retry(self.adapter_name, "exception")
        log.warning(
            f"Download {self.url} failed, retrying {attempt + 1}/{self.policy.max_retries} "
            f"in {delay:.1f}s... Error: {error}"
        )
        return delay

    def give_up(self, attempt: int) -> Response:
        error = self.last_error or Exception("Max retries exceeded")
        return Response.error_response(self.url, error, attempts=attempt + 1)


def _loop_for(host: object, args: tuple[Any, ...], kwargs: dict[str, Any]) -> RetryLoop:
    url = str(args[0]) if args else str(kwargs.get("url", _UNKNOWN_URL))
    adapter_name = str(getattr(host, "adapter_name", _UNKNOWN_ADAPTER))
    return RetryLoop(RetryPolicy.resolve(host, kwargs), url, adapter_name)


def retry() -> Callable[[Callable[..., Response]], Callable[..., Response]]:
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
