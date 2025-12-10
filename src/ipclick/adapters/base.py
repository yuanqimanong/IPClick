# -*- coding:utf-8 -*-

"""
下载器基类和通用装饰器

@time: 2025-12-10
@author: Hades
@file: base.py
"""
import logging
import time
from abc import ABC, abstractmethod
from random import randint
from typing import Optional, Dict, Any, Literal

from ..dto.response import Response

HttpMethod = Literal[
    "GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "TRACE", "PATCH", "QUERY"
]


def retry(max_retries_attr="max_retries", retry_delay_attr="retry_delay"):
    """
    重试装饰器，支持指数退避和随机延迟

    Args:
        max_retries_attr: 最大重试次数属性名
        retry_delay_attr: 重试延迟属性名
    """

    def decorator(func):
        def wrapper(self, *args, **kwargs):
            max_retries = kwargs.get('max_retries') or getattr(self, max_retries_attr, 3)
            retry_delay = kwargs.get('retry_delay') or getattr(self, retry_delay_attr, (1, 3))
            url = args[0] if args else kwargs.get('url', 'unknown')

            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    start_time = time.time()
                    result = func(self, *args, **kwargs)

                    # 设置响应时间
                    if hasattr(result, 'elapsed_ms') and result.elapsed_ms == 0:
                        result.elapsed_ms = int((time.time() - start_time) * 1000)

                    return result

                except Exception as e:
                    last_exception = e

                    if attempt == max_retries:
                        # 最后一次尝试失败，返回错误响应
                        return Response.error_response(url, e)

                    # 计算退避延迟：指数退避 + 随机因子
                    base_delay = min(2 ** attempt, 8)  # 最大8秒基础延迟
                    if isinstance(retry_delay, tuple):
                        random_delay = randint(retry_delay[0], retry_delay[1])
                    else:
                        random_delay = retry_delay

                    sleep_time = base_delay + random_delay
                    sleep_time = min(sleep_time, 30)  # 最大延迟30秒

                    # 记录重试信息
                    if hasattr(self, 'logger'):
                        self.logger.warning(
                            f"Download {url} failed, retrying {attempt + 1}/{max_retries} "
                            f"in {sleep_time}s...  Error: {e}"
                        )

                    time.sleep(sleep_time)

            # 理论上不会到达这里
            return Response.error_response(url, last_exception or Exception("Max retries exceeded"))

        return wrapper

    return decorator


class Downloader(ABC):
    """下载器抽象基类"""

    def __init__(self):
        self.proxy = None
        self.max_retries = 3
        self.retry_delay = (1, 3)
        self.timeout = 30
        self.verify_ssl = True
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

        # 获取日志器
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    @abstractmethod
    def download(self, url: str, *,
                 method: HttpMethod = "GET",
                 params: Optional[Dict] = None,
                 data: Any = None,
                 headers: Optional[Dict] = None,
                 cookies: Optional[Dict] = None,
                 timeout: Optional[float] = None,
                 proxy: Optional[str] = None,
                 **kwargs) -> Response:
        """
        执行HTTP请求

        Args:
            url: 请求URL
            method: HTTP方法
            params: URL参数
            data: 请求体数据
            headers: 请求头
            cookies: Cookie
            timeout: 超时时间
            proxy: 代理地址
            **kwargs: 其他参数

        Returns:
            Response:  统一的响应对象
        """
        pass

    def get(self, url: str, **kwargs) -> Response:
        """GET请求快捷方法"""
        return self.download(url, method="GET", **kwargs)

    def post(self, url: str, **kwargs) -> Response:
        """POST请求快捷方法"""
        return self.download(url, method="POST", **kwargs)

    def put(self, url: str, **kwargs) -> Response:
        """PUT请求快捷方法"""
        return self.download(url, method="PUT", **kwargs)

    def delete(self, url: str, **kwargs) -> Response:
        """DELETE请求快捷方法"""
        return self.download(url, method="DELETE", **kwargs)

    def head(self, url: str, **kwargs) -> Response:
        """HEAD请求快捷方法"""
        return self.download(url, method="HEAD", **kwargs)

    def options(self, url: str, **kwargs) -> Response:
        """OPTIONS请求快捷方法"""
        return self.download(url, method="OPTIONS", **kwargs)

    def close(self):
        """关闭连接，释放资源"""
        pass

    def __enter__(self):
        """上下文管理器入口"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器退出"""
        self.close()
