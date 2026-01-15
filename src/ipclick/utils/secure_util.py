import hashlib
import json
from typing import Any


class SecureUtil:
    """提供安全相关的工具方法的工具类。

    该类包含MD5哈希计算等安全相关的实用方法。
    """

    @staticmethod
    def md5(
        data: int | str | dict[str, Any] | list[Any],
        encoding: str = "utf-8",
        short: bool = False,
    ) -> str:
        """计算给定数据的MD5哈希值。

        Args:
            data: 需要计算MD5哈希的数据，支持整数、字符串、字典或列表类型。
            encoding: 字符串编码格式，默认为"utf-8"。
            short: 是否返回短格式的MD5值(截取第8-24位)，默认为False。

        Returns:
            MD5哈希值的十六进制字符串表示。如果short为True，则返回截取后的16位哈希值；
            否则返回完整的32位哈希值。

        Example:
            >>> SecureUtil.md5("hello world")
            '5d41402abc4b2a76b9719d911017c592'

            >>> SecureUtil.md5({"key": "value"}, short=True)
            'ddce808de0032747'
        """
        if isinstance(data, dict):
            serialized = json.dumps(data, sort_keys=True, separators=(",", ":"))
        else:
            serialized = str(data)

        md5_hash = hashlib.md5()
        md5_hash.update(serialized.encode(encoding))
        return md5_hash.hexdigest()[8:24] if short else md5_hash.hexdigest()
