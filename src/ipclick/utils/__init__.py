from datetime import date, datetime, time
import re
from typing import Any


def json_serializer(obj: Any):
    if isinstance(obj, (datetime, date, time)):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)!r} not serializable")


ISO_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def json_hook(obj: dict[str, Any]) -> dict[str, Any]:
    def json_deserializer(value: Any) -> Any:
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
