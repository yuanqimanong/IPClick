"""集中处理宽松配置读取与严格配置校验的类型转换。"""

from __future__ import annotations

import math

from ipclick.exceptions import ConfigError


_TRUE_WORDS = frozenset({"true", "yes", "on", "1"})
_FALSE_WORDS = frozenset({"false", "no", "off", "0", ""})


def as_bool(value: object, default: bool = False) -> bool:
    """宽松转换布尔值，无法识别时返回默认值。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_WORDS:
            return True
        if text in _FALSE_WORDS:
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def as_int(value: object, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    """宽松转换受上下界约束的整数。"""
    if value is None or isinstance(value, bool):
        return default
    try:
        result = int(value)  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError, OverflowError):
        return default
    if minimum is not None and result < minimum:
        return default
    if maximum is not None and result > maximum:
        return default
    return result


def as_float(value: object, default: float, *, minimum: float | None = None) -> float:
    """宽松转换带可选下界的浮点数。"""
    if value is None or isinstance(value, bool):
        return default
    try:
        result = float(value)  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        return default
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        return default
    return result


def as_positive_float(value: object, default: float) -> float:
    """转换正浮点数，非正或非法输入回退到默认值。"""
    result = as_float(value, default)
    return result if result > 0 else default


def as_text(value: object, default: str = "") -> str:
    """转换并清理非空文本。"""
    if value is None:
        return default
    return str(value).strip() or default


def as_optional_text(value: object) -> str | None:
    """转换可选文本，空白值归一为 ``None``。"""
    return str(value or "").strip() or None


def as_text_tuple(value: object, allowed: frozenset[str] | None = None) -> tuple[str, ...]:
    """把容器中的值归一为小写文本元组，并可按白名单过滤。"""
    if not isinstance(value, (list, tuple, set)):
        return ()
    items = (str(item).strip().lower() for item in value)
    return tuple(item for item in items if item and (allowed is None or item in allowed))


def require_bool(value: object, field: str, default: bool = False) -> bool:
    """严格读取布尔配置，非法输入抛出带字段名的 ``ConfigError``。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_WORDS:
            return True
        if text in _FALSE_WORDS:
            return False
    elif isinstance(value, int):
        return bool(value)
    raise ConfigError(f"{field} 期望布尔值（true/false），得到 {value!r}")


def require_int(value: object, field: str, default: int, *, minimum: int = 0) -> int:
    """严格读取具有下界的整数配置。"""
    if value is None:
        return default
    if isinstance(value, bool):
        raise ConfigError(f"{field} 期望整数，得到布尔值 {value!r}")
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        raise ConfigError(f"{field} 期望整数，得到 {value!r}")
    try:
        result = int(value)  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError, OverflowError):
        raise ConfigError(f"{field} 期望整数，得到 {value!r}") from None
    if result < minimum:
        raise ConfigError(f"{field} 不能小于 {minimum}，得到 {result}")
    return result


def require_float(value: object, field: str, default: float, *, minimum: float = 0.0) -> float:
    """严格读取具有下界的数值配置。"""
    if value is None:
        return default
    if isinstance(value, bool):
        raise ConfigError(f"{field} 期望数字，得到布尔值 {value!r}")
    try:
        result = float(value)  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        raise ConfigError(f"{field} 期望数字，得到 {value!r}") from None
    if not math.isfinite(result):
        raise ConfigError(f"{field} 期望有限数字，得到 {value!r}")
    if result < minimum:
        raise ConfigError(f"{field} 不能小于 {minimum:g}，得到 {result:g}")
    return result


__all__ = [
    "as_bool",
    "as_float",
    "as_int",
    "as_optional_text",
    "as_positive_float",
    "as_text",
    "as_text_tuple",
    "require_bool",
    "require_float",
    "require_int",
]
