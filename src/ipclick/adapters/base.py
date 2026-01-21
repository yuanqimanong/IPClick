from abc import ABC, abstractmethod
from random import randint
import time
from typing import Any

from ipclick.dto import Response


def retry(max_retries_attr="max_retries", retry_delay_attr="retry_delay"):
    """
    重试装饰器，支持指数退避和随机延迟

    Args:
        max_retries_attr: 最大重试次数属性名
        retry_delay_attr: 重试延迟属性名
    """

    def decorator(func):
        def wrapper(self, *args, **kwargs):
            max_retries = kwargs.get("max_retries") or getattr(self, max_retries_attr, 3)
            retry_delay = kwargs.get("retry_delay") or getattr(self, retry_delay_attr, (1, 3))
            url = args[0] if args else kwargs.get("url", "unknown")

            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    start_time = time.time()
                    result = func(self, *args, **kwargs)

                    # 设置响应时间
                    if hasattr(result, "elapsed_ms") and result.elapsed_ms == 0:
                        result.elapsed_ms = int((time.time() - start_time) * 1000)

                    return result

                except Exception as e:
                    last_exception = e

                    if attempt == max_retries or kwargs.get("max_retries") == 0:
                        # 最后一次尝试失败，返回错误响应
                        return Response.error_response(url, e)

                    # 计算退避延迟：指数退避 + 随机因子
                    if kwargs.get("retry_delay") != 0.0:
                        base_delay = min(2**attempt, 600)  # 最大600秒基础延迟
                        if isinstance(retry_delay, tuple):
                            random_delay = randint(retry_delay[0], retry_delay[1])
                        else:
                            random_delay = retry_delay

                        sleep_time = base_delay + random_delay

                        # 记录重试信息
                        if hasattr(self, "logger"):
                            self.logger.warning(
                                f"Download {url} failed, retrying {attempt + 1}/{max_retries} in {sleep_time}s...  Error: {e}"
                            )

                        time.sleep(sleep_time)

            # 理论上不会到达这里
            return Response.error_response(url, last_exception or Exception("Max retries exceeded"))

        return wrapper

    return decorator


class DownloaderAdapter(ABC):
    """下载器抽象基类"""

    adapter_name = "base_downloader_adapter"

    def __init__(self):
        self.proxy = None
        self.max_retries = 3
        self.retry_delay = (1, 3)
        self.timeout = 30
        self.verify_ssl = True

    @abstractmethod
    def download(
        self,
        url: str,
        *,
        # 协议
        method: str = "GET",
        headers: dict[str, Any] | None = None,
        cookies: dict[str, Any] | str | None = None,
        params: dict[str, Any] | None = None,
        data: Any = None,
        json: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        proxy: str | None = None,
        timeout: float = 60,
        max_retries: int = 3,
        retry_delay: float = 2.0,
        verify: bool = True,
        allow_redirects: bool = True,
        stream: bool = False,
        impersonate: str | None = None,
        extensions: dict[str, Any] | None = None,
        # 渲染
        automation_config: str | None = None,
        automation_script: str | None = None,
        allowed_status_codes: list[Any] | None = None,
        kwargs: str | None = None,
    ) -> Response:
        """
        执行HTTP请求

        Args:
            url: 请求URL
            method: 请求方法
            headers: 请求头
            cookies: 请求cookies
            params: 请求参数
            data: 请求数据
            json: 请求JSON数据
            files: 文件上传
            proxy: 代理地址
            timeout: 超时时间
            max_retries: 最大重试次数
            retry_delay: 重试退避因子
            verify: SSL证书验证
            allow_redirects: 允许重定向
            stream: 是否流式读取
            impersonate: 伪装身份
            extensions: 扩展参数
            automation_config: 自动化配置
            automation_script: 自动化脚本
            allowed_status_codes: 允许的状态码
            **kwargs: 扩展参数

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
