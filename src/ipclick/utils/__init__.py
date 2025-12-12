# -*- coding:utf-8 -*-

"""
@time: 2025-12-10
@author: Hades
@file: __init__.py
"""
import hashlib
import json


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
