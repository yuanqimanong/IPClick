# -*- coding:utf-8 -*-

"""
@time: 2025-12-22
@author: Hades
@file: proxy_usage.py

三种代理写法。请把 PROXY_URL / ProxyConfig 换成你自己可用的代理。
"""

from examples.base_config import HTTPBIN_IP_URL
from ipclick import ProxyConfig, downloader


PROXY_URL = "http://127.0.0.1:7890"


def _show(label: str, response) -> None:
    """代理不可用时 response.text 是空的，这里把 error 一并打出来。"""
    if response.is_success():
        print(f"{label}: {response.text.strip()}")
    else:
        print(f"{label}: 失败 (status={response.status_code}) {response.error}")


def proxy_bool():
    print("=== 使用配置代理（proxy=True）===")
    _show("配置代理", downloader.get(HTTPBIN_IP_URL, proxy=True, max_retries=0))


def proxy_string():
    print("=== 使用代理字符串 ===")
    _show("字符串代理", downloader.get(HTTPBIN_IP_URL, proxy=PROXY_URL, max_retries=0))


def proxy_config_obj():
    print("=== ProxyConfig 对象 ===")
    proxy = ProxyConfig(scheme="http", host="127.0.0.1", port=7890)
    _show("ProxyConfig", downloader.get(HTTPBIN_IP_URL, proxy=proxy, max_retries=0))


def no_proxy():
    print("=== 不使用代理（对照）===")
    _show("直连", downloader.get(HTTPBIN_IP_URL, max_retries=0))


if __name__ == "__main__":
    no_proxy()
    proxy_bool()
    proxy_string()
    proxy_config_obj()
