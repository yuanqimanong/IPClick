# -*- coding:utf-8 -*-

"""
@time: 2025-12-22
@author: Hades
@file: advanced_options.py
"""
from examples.base_config import BASE_URL
from ipclick import downloader


def timeout_example():
    print("=== 超时设置（timeout=1）===")
    resp = downloader.get(f"{BASE_URL}/delay/2", timeout=1, max_retries=0, retry_backoff=0)
    if resp.status_code == -1:
        print(resp.request_uuid)
        print(resp.adapter_type)
        print(resp.error)


def retries_example():
    print("=== 最大重试次数，退避指数 ===")
    # 用不通的URL故意出错
    resp = downloader.get("http://localhost:9999/unreachable", max_retries=5, retry_backoff=1)
    if resp.status_code == -1:
        print(resp.request_uuid)
        print(resp.adapter_type)
        print(resp.error)


def redirect_example():
    print("=== 允许/禁止重定向 ===")
    # httpbin /redirect-to?url=xxx 可以配合用
    r1 = downloader.get(f"{BASE_URL}/redirect-to?url={BASE_URL}/get", allow_redirects=True)
    print(f"开启重定向后: {r1.status_code} - {r1.url} - {r1.headers.get('location')} - {r1.text}")
    r2 = downloader.get(f"{BASE_URL}/redirect-to?url={BASE_URL}/get", allow_redirects=False)
    print(f"禁止重定向后:  {r2.status_code} - {r2.url} - {r2.headers.get('location')} - {r2.text}")


if __name__ == "__main__":
    timeout_example()
    retries_example()
    redirect_example()
