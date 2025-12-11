# -*- coding:utf-8 -*-

"""
curl_cffi下载器适配器 - 默认推荐的HTTP客户端

@time: 2025-12-10
@author: Hades
@file: curl_cffi_adapter.py
"""
from copy import deepcopy
from typing import Optional, Dict, Any

from ipclick import Downloader, HttpMethod
from ipclick.adapters.base import retry
from ipclick.dto import Response

try:
    import curl_cffi.requests
    from curl_cffi.requests.impersonate import DEFAULT_CHROME

    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False
    DEFAULT_CHROME = None

try:
    from fake_useragent import UserAgent

    FAKE_UA_AVAILABLE = True
except ImportError:
    FAKE_UA_AVAILABLE = False


class CurlCffiAdapter(Downloader):
    """
    curl_cffi适配器，支持浏览器指纹伪装

    优势：
    - 更好的反检测能力
    - 浏览器指纹伪装
    - 更快的性能
    - 支持HTTP/2
    """

    def __init__(self):
        if not CURL_CFFI_AVAILABLE:
            raise ImportError(
                "curl_cffi is not installed. Install it with: pip install curl-cffi"
            )

        super().__init__()

        # HTTP方法映射
        self.method_mapping = {
            'GET': curl_cffi.requests.get,
            'POST': curl_cffi.requests.post,
            'PUT': curl_cffi.requests.put,
            'DELETE': curl_cffi.requests.delete,
            'PATCH': curl_cffi.requests.patch,
            'HEAD': curl_cffi.requests.head,
            'OPTIONS': curl_cffi.requests.options,
        }

        # curl_cffi特有配置
        self.impersonate = DEFAULT_CHROME
        self.ja3 = None
        self.akamai = None
        self.session = None

        # User Agent生成器
        if FAKE_UA_AVAILABLE:
            self.ua_generator = UserAgent(platforms='desktop')
        else:
            self.ua_generator = None

    def get_session(self, **kwargs):
        """获取或创建会话"""
        if self.session is None:
            self.session = curl_cffi.requests.Session(**kwargs)
        return self.session

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
        使用curl_cffi执行HTTP请求
        """
        tmp_kwargs = deepcopy(kwargs)
        method = method.upper()

        # 设置代理
        proxies = None
        if proxy:
            proxies = {'http': proxy, 'https': proxy}

        # 设置浏览器指纹伪装
        if 'impersonate' not in tmp_kwargs:
            tmp_kwargs['impersonate'] = self.impersonate or DEFAULT_CHROME

        # 清理不需要传递给curl_cffi的参数
        for key in ['max_retries', 'retry_delay', 'verify']:
            tmp_kwargs.pop(key, None)

        # 设置默认headers
        if headers is None:
            headers = {}

        # 自动设置User-Agent
        # if 'User-Agent' not in headers and 'user-agent' not in headers:
        #     headers['User-Agent'] = self._get_user_agent()

        # 设置超时
        request_timeout = timeout

        # SSL验证处理
        if 'verify' in kwargs:
            # curl_cffi使用不同的参数名
            if not kwargs['verify']:
                tmp_kwargs['verify'] = False

        # impersonate
        impersonate = tmp_kwargs['impersonate']

        try:
            # 获取请求方法
            request_func = self.method_mapping.get(method)
            if not request_func:
                raise ValueError(f"Unsupported HTTP method: {method}")

            # 执行请求
            curl_cffi_resp = request_func(
                url=url,
                params=params,
                data=data,
                headers=headers,
                cookies=cookies,
                timeout=request_timeout,
                proxies=proxies,
                impersonate=impersonate,
            )

            # 转换响应
            return Response(
                url=str(curl_cffi_resp.url),
                status_code=curl_cffi_resp.status_code,
                content=curl_cffi_resp.content,
                text=curl_cffi_resp.text,
                headers=dict(curl_cffi_resp.headers),
                raw_response=curl_cffi_resp
            )

        except Exception as e:
            self.logger.error(f"curl_cffi request failed for {url}: {e}")
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
    """检查curl_cffi是否可用"""
    return CURL_CFFI_AVAILABLE
