import hashlib
import json
from typing import Any


class SecureUtil:
    @staticmethod
    def md5(
        data: int | str | dict[str, Any] | list[Any],
        encoding: str = "utf-8",
        short: bool = False,
    ) -> str:
        result_cache: list[str] = []

        data_list: list[Any] = data if isinstance(data, list) else [data]

        for _d in data_list:
            result = json.dumps(_d, sort_keys=True, separators=(",", ":")) if isinstance(_d, dict) else str(_d)
            result_cache.append(result)

        serialized = "".join(result_cache)
        md5_hash = hashlib.md5(serialized.encode(encoding), usedforsecurity=False)

        return md5_hash.hexdigest()[8:24] if short else md5_hash.hexdigest()
