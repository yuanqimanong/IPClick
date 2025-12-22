# -*- coding:utf-8 -*-

"""
curl_cffi下载器适配器 - 默认推荐的HTTP客户端

@time: 2025-12-10
@author: Hades
@file: curl_cffi_adapter.py
"""
import json as json_lib
from typing import Optional, Dict, Any, List

from ipclick.adapters.base import retry, Downloader
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
    def download(self, url: str,
                 *,
                 method: str = "GET",
                 headers: Optional[Dict[str, Any]] = None,
                 cookies: Optional[Dict[str, Any], str] = None,
                 params: Optional[Dict[str, Any]] = None,
                 data: Any = None,
                 json: Optional[Dict[str, Any]] = None,
                 files: Optional[Dict[str, Any]] = None,
                 proxy: str = None,
                 timeout: float = 60,
                 max_retries: int = 3,
                 retry_delay: float = 2.0,
                 verify: bool = True,
                 allow_redirects: bool = True,
                 stream: bool = False,
                 impersonate: Optional[str] = None,
                 extensions: Optional[Dict[str, Any]] = None,
                 automation_config: str = None,
                 automation_script: str = None,
                 allowed_status_codes: Optional[List[int]] = None,
                 kwargs: str = None) -> Response:
        """
        使用curl_cffi执行HTTP请求
        """
        kwargs_dict = json_lib.loads(kwargs)
        method = method.upper()

        # 设置代理
        proxies = None
        if proxy:
            proxies = {'http': proxy, 'https': proxy}

        try:
            # 获取请求方法
            request_func = self.method_mapping.get(method)
            if not request_func:
                raise ValueError(f"Unsupported HTTP method: {method}")

            # 执行请求
            curl_cffi_resp = request_func(
                url=url,
                headers=headers,
                cookies=cookies,
                params=params,
                data=data,
                json=json,
                proxies=proxies,
                timeout=timeout,
                verify=verify,
                allow_redirects=allow_redirects,
                stream=stream,
                impersonate=impersonate or DEFAULT_CHROME,
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
