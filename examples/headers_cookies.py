# -*- coding:utf-8 -*-

"""
@time: 2025-12-19
@author: Hades
@file: headers_cookies.py
"""

from examples.base_config import HTTPBIN_HEADERS_URL
from ipclick import downloader


def custom_headers():
    print("=== 自定义 Headers ===")
    headers = {"X-Demo-Header": "ipclick", "User-Agent": "IPClick-Test"}
    response = downloader.get(HTTPBIN_HEADERS_URL, headers=headers)
    print(f"响应内容: {response.text}")


# TODO
def custom_cookies():
    print("=== 自定义 Cookies ===")
    cookies = {"session": "abc123", "test_flag": "yes"}
    response = downloader.get(HTTPBIN_HEADERS_URL, cookies=cookies)
    print(f"响应内容: {response.text}")


if __name__ == "__main__":
    custom_headers()
    custom_cookies()
