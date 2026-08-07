from abc import ABC, abstractmethod
from collections.abc import Callable
import functools
from random import uniform
import time
from typing import Any

from ipclick.dto.response import Response
from ipclick.utils.log_util import log


# 单次重试等待的上限（秒）。服务端每个请求占用一个 gRPC worker 线程，
# 原来的 min(2**attempt, 600) 在 max_retries 稍大时会让线程睡上十分钟。
MAX_RETRY_DELAY = 30.0

# 默认触发重试的状态码（连接层异常总是会重试，与此无关）
DEFAULT_RETRY_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})


def _coerce_delay(value: Any, default: float) -> float:
    """把 retry_delay 归一成秒数。

    历史上它既可能是 float（服务端传来的 retry_backoff_seconds），
    也可能是 (min, max) 元组（适配器自身的默认值）。
    """
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


def retry(max_retries_attr: str = "max_retries", retry_delay_attr: str = "retry_delay") -> Callable[..., Any]:
    """
    重试装饰器，支持指数退避和随机延迟

    Args:
        max_retries_attr: 最大重试次数属性名
        retry_delay_attr: 重试延迟属性名
    """

    def decorator(func: Callable[..., Response]) -> Callable[..., Response]:
        @functools.wraps(func)
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Response:
            # 用 `is None` 判断而不是 `or`：max_retries=0 / retry_delay=0
            # 是合法取值（表示"不重试"/"不等待"），用 `or` 会被当成未传而回落到默认值。
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
            retry_codes = DEFAULT_RETRY_STATUS_CODES

            last_exception: Exception | None = None

            for attempt in range(max_retries + 1):
                start_time = time.monotonic()
                try:
                    result = func(self, *args, **kwargs)

                    if hasattr(result, "elapsed_ms") and result.elapsed_ms == 0:
                        result.elapsed_ms = int((time.monotonic() - start_time) * 1000)

                    # 状态码级重试：allowed_status_codes 给出的是"可接受"的状态码，
                    # 其余落在重试名单里的（429/5xx）值得再试一次。
                    status = getattr(result, "status_code", None)
                    if (
                        attempt < max_retries
                        and isinstance(status, int)
                        and status in retry_codes
                        and not (allowed and status in allowed)
                    ):
                        sleep_time = _backoff(attempt, base_delay)
                        log.warning(
                            f"Download {url} returned {status}, "
                            f"retrying {attempt + 1}/{max_retries} in {sleep_time:.1f}s..."
                        )
                        time.sleep(sleep_time)
                        continue

                    return result

                except Exception as e:
                    last_exception = e

                    if attempt >= max_retries:
                        return Response.error_response(url, e)

                    sleep_time = _backoff(attempt, base_delay)
                    # 原来这行日志裹在 `if hasattr(self, "logger")` 里，而适配器
                    # 从来没有 logger 属性，等于重试全程静默。
                    log.warning(
                        f"Download {url} failed, retrying {attempt + 1}/{max_retries} "
                        f"in {sleep_time:.1f}s... Error: {e}"
                    )
                    time.sleep(sleep_time)

            return Response.error_response(url, last_exception or Exception("Max retries exceeded"))

        return wrapper

    return decorator


def _backoff(attempt: int, base_delay: float) -> float:
    """指数退避 + 抖动，并封顶到 MAX_RETRY_DELAY。

    抖动可以避免多个并发任务在同一时刻集体重试（惊群）。
    """
    delay = min(base_delay * (2**attempt), MAX_RETRY_DELAY)
    return delay * uniform(0.8, 1.2)


class DownloaderAdapter(ABC):
    """下载器抽象基类"""

    adapter_name: str = "base_downloader_adapter"

    def __init__(self):
        self.proxy: str | None = None
        self.max_retries: int = 3
        self.retry_delay: float = 1.0
        self.timeout: float = 30
        self.verify_ssl: bool = True
        # 兜底 UA：fake_useragent 不可用或抛错时使用
        self.user_agent: str = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

    @abstractmethod
    def download(
        self,
        url: str,
        *,
        # 协议
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
        extensions: dict[str, Any] | None = None,
        # 渲染
        automation_config: str | None = None,
        automation_script: str | None = None,
        allowed_status_codes: list[Any] | None = None,
        kwargs: str | None = None,
    ) -> Response:
        """
        执行HTTP请求

        Args:
            url: 请求URL
            method: 请求方法
            headers: 请求头
            cookies: 请求cookies
            params: 请求参数
            data: 请求数据
            json: 请求JSON数据
            files: 文件上传
            proxy: 代理地址
            timeout: 超时时间
            max_retries: 最大重试次数
            retry_delay: 重试退避基数（秒）
            verify: SSL证书验证
            allow_redirects: 允许重定向
            stream: 是否流式读取
            impersonate: 伪装身份
            extensions: 扩展参数
            automation_config: 自动化配置
            automation_script: 自动化脚本
            allowed_status_codes: 可接受的状态码（不触发重试）
            kwargs: 透传给底层客户端的额外参数（JSON 字符串）

        Returns:
            Response: 统一的响应对象
        """
        raise NotImplementedError

    @staticmethod
    def parse_extra_kwargs(raw: str | None) -> dict[str, Any]:
        """解析透传的 kwargs JSON 字符串。

        SDK 总会发 ``"{}"``，但直接构造 DownloadTask 或用第三方 gRPC 客户端时
        可能是 ``None`` / 空串，此时不应该抛 JSONDecodeError。
        """
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
        """获取User-Agent，fake_useragent 不可用时回退到内置 UA。"""
        generator = getattr(self, "ua_generator", None)
        if generator is not None:
            try:
                return str(generator.random)
            except Exception:
                log.debug("fake_useragent 取值失败，使用内置 User-Agent")
        return self.user_agent

    def get(self, url: str, **kwargs: Any) -> Response:
        """GET请求快捷方法"""
        return self.download(url, method="GET", **kwargs)

    def post(self, url: str, **kwargs: Any) -> Response:
        """POST请求快捷方法"""
        return self.download(url, method="POST", **kwargs)

    def put(self, url: str, **kwargs: Any) -> Response:
        """PUT请求快捷方法"""
        return self.download(url, method="PUT", **kwargs)

    def delete(self, url: str, **kwargs: Any) -> Response:
        """DELETE请求快捷方法"""
        return self.download(url, method="DELETE", **kwargs)

    def head(self, url: str, **kwargs: Any) -> Response:
        """HEAD请求快捷方法"""
        return self.download(url, method="HEAD", **kwargs)

    def options(self, url: str, **kwargs: Any) -> Response:
        """OPTIONS请求快捷方法"""
        return self.download(url, method="OPTIONS", **kwargs)

    def close(self) -> None:  # noqa: B027 - 基类默认无资源可关，子类按需覆写
        """关闭连接，释放资源"""

    def __enter__(self) -> "DownloaderAdapter":
        """上下文管理器入口"""
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        """上下文管理器退出"""
        self.close()
