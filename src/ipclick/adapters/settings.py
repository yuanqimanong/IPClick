from dataclasses import dataclass
from typing import Any


DEFAULT_RETRY_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

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
    connect_timeout: float = 10.0
    download_timeout: float = 300.0

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
