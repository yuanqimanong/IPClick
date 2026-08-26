"""HTTP 适配器共享的连接、超时和重试配置。"""

from dataclasses import dataclass
from typing import Any

from ipclick.utils.coerce import as_int, as_positive_float
from ipclick.utils.config_util import section


DEFAULT_RETRY_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

# 超时的**唯一真源**：随包配置模板、适配器 dataclass、各适配器 download() 的签名默认值、
# Web 的可编辑字段默认值，全部指向这两个常量。此前它们各写各的（settings 里 300、
# download() 签名里 60、页面预填 30），"默认超时是多少"这个问题有三个不同的答案。
#
# 连接段是从总预算里**先**划走的（见 curl_cffi_adapter._timeout_pair），所以两者不是
# 并列关系：连接给得越多，留给收数据的越少。20 / 60 这组值是按"走隧道代理"定的——
# 建连要等代理再去拨目标站，比直连慢得多。
DEFAULT_CONNECT_TIMEOUT = 20.0

DEFAULT_DOWNLOAD_TIMEOUT = 60.0

# 流式下载单独一份、且大得多的预算：整段响应体要在这一个预算里收完，而流式用的正是
# "文件大、收得久"的场景。和普通请求共用 60 秒的话，默认配置下大文件必然中途断，
# 错误信息还只会说"超时"——看不出预算是按普通请求给的。
DEFAULT_STREAM_TIMEOUT = 300.0

HARD_MAX_BACKOFF = 300.0

HARD_MAX_RETRIES = 20


@dataclass(frozen=True)
class AdapterSettings:
    """适配器创建时固定的连接池与重试默认值。"""

    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    download_timeout: float = DEFAULT_DOWNLOAD_TIMEOUT
    stream_timeout: float = DEFAULT_STREAM_TIMEOUT

    max_attempts: int = 3
    backoff_exponent: float = 2.0
    initial_backoff: float = 1.0
    max_backoff: float = 30.0
    retry_codes: frozenset[int] = DEFAULT_RETRY_STATUS_CODES

    max_connections: int = 100
    max_keepalive_connections: int = 20

    trust_env: bool = False

    @classmethod
    def from_config(cls, downloader_config: dict[str, Any] | None) -> "AdapterSettings":
        """从 ``[DOWNLOADER]`` 配置解析容错后的适配器设置。"""
        config = dict(downloader_config or {})
        retry = section(config, "retry")
        concurrency = section(config, "concurrency")

        codes = retry.get("retry_codes")
        retry_codes = (
            frozenset(int(c) for c in codes if str(c).lstrip("-").isdigit())
            if isinstance(codes, (list, tuple, set)) and codes
            else DEFAULT_RETRY_STATUS_CODES
        )

        defaults = cls()
        return cls(
            connect_timeout=as_positive_float(config.get("connect_timeout"), defaults.connect_timeout),
            download_timeout=as_positive_float(config.get("download_timeout"), defaults.download_timeout),
            stream_timeout=as_positive_float(config.get("stream_timeout"), defaults.stream_timeout),
            max_attempts=as_int(retry.get("max_attempts"), defaults.max_attempts, minimum=0, maximum=HARD_MAX_RETRIES),
            backoff_exponent=as_positive_float(retry.get("backoff_exponent"), defaults.backoff_exponent),
            initial_backoff=as_positive_float(retry.get("initial_backoff"), defaults.initial_backoff),
            max_backoff=min(as_positive_float(retry.get("max_backoff"), defaults.max_backoff), HARD_MAX_BACKOFF),
            retry_codes=retry_codes,
            max_connections=as_int(concurrency.get("max_connections"), defaults.max_connections, minimum=1),
            max_keepalive_connections=as_int(
                concurrency.get("max_keepalive_connections"), defaults.max_keepalive_connections, minimum=1
            ),
            trust_env=bool(config.get("trust_env", defaults.trust_env)),
        )


__all__ = ["DEFAULT_RETRY_STATUS_CODES", "HARD_MAX_BACKOFF", "HARD_MAX_RETRIES", "AdapterSettings"]
