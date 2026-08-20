"""提供用于缓存键等非安全场景的稳定摘要。"""

import hashlib
import json
from typing import Any


class SecureUtil:
    """兼容旧调用点的摘要工具命名空间。"""

    @staticmethod
    def md5(
        data: int | str | dict[str, Any] | list[Any],
        encoding: str = "utf-8",
        short: bool = False,
    ) -> str:
        """生成保留列表元素边界的稳定 MD5；禁止用于密码或签名。"""
        if isinstance(data, (dict, list)):
            serialized = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        else:
            serialized = str(data)
        # 显式声明非安全用途，兼容启用 FIPS 的 Python 运行环境。
        md5_hash = hashlib.md5(serialized.encode(encoding), usedforsecurity=False)

        return md5_hash.hexdigest()[8:24] if short else md5_hash.hexdigest()
