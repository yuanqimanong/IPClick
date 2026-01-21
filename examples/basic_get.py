# -*- coding:utf-8 -*-

"""
@time: 2025-12-11
@author: Hades
@file: basic_get.py
"""

from datetime import datetime

from examples.base_config import HTTPBIN_GET_URL
from ipclick import IPClickAdapter, downloader


def basic_get_example():
    print("=== 基础 GET ===")
    # response = downloader.get(HTTPBIN_GET_URL)
    response = downloader.get(HTTPBIN_GET_URL, adapter=IPClickAdapter.HTTPX)
    print(f"状态码: {response.status_code}")
    print(f"响应时间: {response.elapsed_ms}ms")
    print(f"响应内容: {response.text}")


def get_params_example():
    print("=== 参数 GET ===")
    response = downloader.get(
        HTTPBIN_GET_URL,
        params={"first": "lao tie", "second": 666, "third": datetime.now()},
    )
    print(f"状态码: {response.status_code}")
    print(f"响应时间: {response.elapsed_ms}ms")
    print(f"响应内容: {response.text}")


if __name__ == "__main__":
    basic_get_example()
    get_params_example()
