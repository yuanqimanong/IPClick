# -*- coding:utf-8 -*-

"""
@time: 2025-12-22
@author: Hades
@file: proxy_usage.py
"""
from examples.base_config import HTTPBIN_IP_URL
from ipclick import downloader, ProxyConfig


def proxy_bool():
    print("=== 使用配置代理（proxy=True）===")
    response = downloader.get(HTTPBIN_IP_URL, proxy=True)
    print(f"响应内容: {response.text}")


def proxy_string():
    print("=== 使用代理字符串 ===")
    # 示例 proxy 地址，需替换成你实际可用的代理
    proxy_str = "http://127.0.0.1:7890"
    try:
        response = downloader.get(HTTPBIN_IP_URL, proxy=proxy_str)
        print(f"响应内容: {response.text}")
    except Exception as e:
        print(f"代理请求失败: {e}")


def proxy_config_obj():
    print("=== ProxyConfig 对象 ===")
    proxy = ProxyConfig(
        scheme="http",
        host="127.0.0.1",
        port=7890,
    )
    try:
        response = downloader.get(HTTPBIN_IP_URL, proxy=proxy)
        print(f"响应内容: {response.text}")
    except Exception as e:
        print(f"代理对象请求失败: {e}")


if __name__ == "__main__":
    proxy_bool()
    proxy_string()
    proxy_config_obj()
