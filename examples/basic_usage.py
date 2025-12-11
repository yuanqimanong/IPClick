# -*- coding:utf-8 -*-

"""
@time: 2025-12-11
@author: Hades
@file: basic_usage.py
"""
from ipclick import downloader


def basic_get_example():
    """基础GET请求示例"""
    print("=== 基础GET请求示例 ===")

    try:
        # 最简单的GET请求
        response = downloader.get("http://192.168.14.60:9527/get")
        print(f"状态码:  {response.status_code}")
        print(f"响应时间: {response.elapsed_ms}ms")
        print(f"响应内容: {response.json()}")
        print()

    except Exception as e:
        print(f"请求失败: {e}")


def main():
    basic_get_example()


if __name__ == "__main__":
    main()
