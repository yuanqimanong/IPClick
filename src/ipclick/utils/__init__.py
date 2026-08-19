"""跨模块复用的 JSON、日志、路径、安全与配置辅助函数。"""

from datetime import date, datetime, time
import re
from typing import Any


def json_serializer(obj: Any) -> str:
    """将日期和时间对象转换为 ISO 文本，供 ``json.dumps`` 回调使用。"""
    if isinstance(obj, (datetime, date, time)):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)!r} not serializable")


ISO_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def json_hook(obj: dict[str, Any]) -> dict[str, Any]:
    """将 JSON 对象第一层中形似 ISO 的文本还原为 ``datetime``。"""

    def json_deserializer(value: Any) -> Any:
        """尝试解析单个值，无法识别时保持原值。"""
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value).replace(microsecond=0)
            except Exception:
                if ISO_DATETIME_RE.match(value):
                    try:
                        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
                    except Exception:
                        pass
        return value

    for k, v in obj.items():
        obj[k] = json_deserializer(v)
    return obj
