# -*- coding:utf-8 -*-

"""
@time: 2025-12-11
@author: Hades
@file: basic_get.py
"""
from examples.base_config import HTTPBIN_POST_URL
from ipclick import downloader


def post_form_example():
    print("=== 表单 POST ===")
    response = downloader.post(HTTPBIN_POST_URL, data={'foo': 'bar'})
    print(f"响应内容: {response.text}")


def post_json_example():
    print("=== JSON POST ===")
    response = downloader.post(HTTPBIN_POST_URL, json={'ping': 'pong'})
    print(f"响应内容: {response.text}")


if __name__ == "__main__":
    post_form_example()
    post_json_example()
