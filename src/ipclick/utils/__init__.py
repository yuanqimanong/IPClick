# -*- coding:utf-8 -*-

"""
@time: 2025-12-10
@author: Hades
@file: __init__.py
"""
import hashlib
import json
import re
from datetime import datetime, date, time
from typing import Any


class MD5:
    @staticmethod
    def encrypt(data):
        # 处理字典类型，保持键排序一致
        if isinstance(data, dict):
            # 确保字典序列化的一致性
            serialized = json.dumps(data, sort_keys=True, separators=(',', ':'))
        else:
            # 统一转为字符串处理
            serialized = str(data)

        # 创建MD5哈希对象
        md5_hash = hashlib.md5()
        # 使用UTF-8编码更新哈希对象
        md5_hash.update(serialized.encode('utf-8'))
        # 返回32位小写十六进制字符串
        return md5_hash.hexdigest()


# 自定义JSON序列化器
def json_serializer(obj: Any):
    if isinstance(obj, (datetime, date, time)):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)!r} not serializable")


# ISO日期时间正则表达式
ISO_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


# 自定义JSON反序列化器
def json_hook(obj: Any):
    def json_deserializer(value):
        if isinstance(value, str):
            try:
                # 处理带/不带微秒与时区的情况，优先用 fromisoformat（Py3.7+）
                return datetime.fromisoformat(value).replace(microsecond=0)
            except Exception:
                # 备用：简单日期检测
                if ISO_DATETIME_RE.match(value):
                    try:
                        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
                    except Exception:
                        pass
        return value

    for k, v in obj.items():
        obj[k] = json_deserializer(v)
    return obj
