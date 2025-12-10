# -*- coding:utf-8 -*-

"""
httpx下载器适配器 - 备选HTTP客户端

@time: 2025-12-10
@author: Hades
@file: httpx_adapter.py
"""
from typing import Optional, Dict, Any

from ipclick import Downloader, HttpMethod
from ipclick.adapters.base import retry
from ipclick.dto import Response

try:
    import httpx

    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False

try:
    from fake_useragent import UserAgent

    FAKE_UA_AVAILABLE = True
except ImportError:
    FAKE_UA_AVAILABLE = False


class HttpxAdapter(Downloader):
    """
    httpx适配器 - 现代HTTP客户端

    特点：
    - 支持异步操作
    - HTTP/2支持
    - 完善的API
    """

    def __init__(self):
        if not HTTPX_AVAILABLE:
            raise ImportError(
                "httpx is not installed. Install it with: pip install httpx"
            )

        super().__init__()
        self.session = None

        # User Agent生成器
        if FAKE_UA_AVAILABLE:
            self.ua_generator = UserAgent(platforms='desktop')
        else:
            self.ua_generator = None

    def get_session(self, **kwargs):
        """获取或创建会话"""
        if self.session is None:
            self.session = httpx.Client(**kwargs)
        return self.session

    def _get_user_agent(self) -> str:
        """获取User-Agent"""
        if self.ua_generator:
            try:
                return self.ua_generator.random
            except:
                pass
        return self.user_agent

    @retry()
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
        使用httpx执行HTTP请求
        """
        method = method.upper()

        # 设置默认headers
        if headers is None:
            headers = {
                'User-Agent': self._get_user_agent(),
                'Accept': '*/*',
                'Accept-Encoding': 'gzip, deflate, br',
                'Connection': 'keep-alive',
            }
        elif 'User-Agent' not in headers and 'user-agent' not in headers:
            headers['User-Agent'] = self._get_user_agent()

        # 构建httpx请求参数
        httpx_kwargs = {
            'params': params,
            'data': data,
            'headers': headers,
            'cookies': cookies,
            'timeout': timeout or self.timeout,
        }

        # 设置代理
        if proxy:
            httpx_kwargs['proxies'] = proxy

        # 添加其他参数
        supported_kwargs = [
            'content', 'files', 'json', 'auth', 'follow_redirects',
            'verify', 'cert', 'trust_env'
        ]
        for key in supported_kwargs:
            if key in kwargs:
                httpx_kwargs[key] = kwargs[key]

        # SSL验证
        if 'verify' not in httpx_kwargs:
            httpx_kwargs['verify'] = self.verify_ssl

        # 默认跟随重定向
        if 'follow_redirects' not in httpx_kwargs:
            httpx_kwargs['follow_redirects'] = True

        # 移除None值
        httpx_kwargs = {k: v for k, v in httpx_kwargs.items() if v is not None}

        try:
            # 执行请求
            httpx_resp = httpx.request(method, url, **httpx_kwargs)

            # 转换响应
            return Response(
                url=str(httpx_resp.url),
                status_code=httpx_resp.status_code,
                content=httpx_resp.content,
                text=httpx_resp.text,
                headers=dict(httpx_resp.headers),
                raw_response=httpx_resp
            )

        except Exception as e:
            self.logger.error(f"httpx request failed for {url}: {e}")
            raise

    def close(self):
        """关闭会话"""
        if self.session:
            try:
                self.session.close()
            except:
                pass
            self.session = None


def is_available() -> bool:
    """检查httpx是否可用"""
    return HTTPX_AVAILABLE
