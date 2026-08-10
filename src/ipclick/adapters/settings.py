"""适配器运行参数。

把配置文件的 ``[DOWNLOADER]`` 节变成适配器真正会读的对象。此前这一节只是被
``TaskService`` 存进 ``self.adapter_config`` 就再没被读过，改了完全不生效。

这里只收录能落到 httpx / curl_cffi 上的项。做不到的（按主机的并发上限、限速、
分块下载）不做假映射——宁可在 README 里标"尚未实现"，也不要让人以为配了有用。
"""

from dataclasses import dataclass
from typing import Any


# 默认触发重试的状态码（连接层异常总是会重试，与此无关）
DEFAULT_RETRY_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

# 单次重试等待的硬上限（秒）。服务端每个请求占一个 gRPC worker 线程，
# 退避时间过长会把线程池拖垮，因此即使配置写得更大也按此封顶。
HARD_MAX_BACKOFF = 300.0


def _as_float(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result > 0 else default


def _as_int(value: Any, default: int, *, minimum: int = 0) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result if result >= minimum else default


@dataclass(frozen=True)
class AdapterSettings:
    """适配器的默认行为。请求级参数优先于这里的值。"""

    # 超时
    connect_timeout: float = 10.0
    download_timeout: float = 300.0

    # 重试：sleep = min(initial_backoff * exponent**attempt, max_backoff) * 抖动
    max_attempts: int = 3
    backoff_exponent: float = 2.0
    initial_backoff: float = 1.0
    max_backoff: float = 30.0
    retry_codes: frozenset[int] = DEFAULT_RETRY_STATUS_CODES

    # 连接池
    max_connections: int = 100
    max_keepalive_connections: int = 20

    # 是否读取环境变量里的代理设置
    trust_env: bool = False

    @classmethod
    def from_config(cls, downloader_config: dict[str, Any] | None) -> "AdapterSettings":
        """从配置文件的 ``[DOWNLOADER]`` 节构造。缺失或非法的项回落到默认值。"""
        config = dict(downloader_config or {})
        retry = dict(config.get("retry") or {})
        concurrency = dict(config.get("concurrency") or {})

        codes = retry.get("retry_codes")
        retry_codes = (
            frozenset(int(c) for c in codes if str(c).lstrip("-").isdigit())
            if isinstance(codes, (list, tuple, set)) and codes
            else DEFAULT_RETRY_STATUS_CODES
        )

        defaults = cls()
        return cls(
            connect_timeout=_as_float(config.get("connect_timeout"), defaults.connect_timeout),
            download_timeout=_as_float(config.get("download_timeout"), defaults.download_timeout),
            max_attempts=_as_int(retry.get("max_attempts"), defaults.max_attempts),
            backoff_exponent=_as_float(retry.get("backoff_exponent"), defaults.backoff_exponent),
            initial_backoff=_as_float(retry.get("initial_backoff"), defaults.initial_backoff),
            max_backoff=min(_as_float(retry.get("max_backoff"), defaults.max_backoff), HARD_MAX_BACKOFF),
            retry_codes=retry_codes,
            max_connections=_as_int(concurrency.get("max_connections"), defaults.max_connections, minimum=1),
            max_keepalive_connections=_as_int(
                concurrency.get("max_keepalive_connections"), defaults.max_keepalive_connections, minimum=1
            ),
            trust_env=bool(config.get("trust_env", defaults.trust_env)),
        )


__all__ = ["DEFAULT_RETRY_STATUS_CODES", "HARD_MAX_BACKOFF", "AdapterSettings"]
