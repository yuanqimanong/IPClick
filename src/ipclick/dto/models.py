# -*- coding:utf-8 -*-

"""
@time: 2025-12-10
@author: Hades
@file: models.py
"""

import json
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, Any, Union


class HttpMethod(Enum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"
    PATCH = "PATCH"
    HEAD = "HEAD"
    OPTIONS = "OPTIONS"


class Adapter(Enum):
    CURL_CFFI = "curl_cffi"  # 新增：默认适配器
    HTTPX = "httpx"
    REQUESTS = "requests"
    AIOHTTP = "aiohttp"


@dataclass
class ProxyConfig:
    """代理配置"""
    scheme: str = "http"
    host: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None

    def to_url(self) -> Optional[str]:
        """转换为代理URL"""
        if not self.host:
            return None
        auth = f"{self.username}:{self.password}@" if self.username else ""
        return f"{self.scheme}://{auth}{self.host}:{self.port}"

    def __post_init__(self):
        if self.host and not self.port:
            self.port = 8080  # 默认代理端口


@dataclass
class DownloadTask:
    """用户友好的下载任务"""
    url: str
    method: HttpMethod = HttpMethod.GET
    headers: Optional[Dict[str, str]] = field(default_factory=dict)
    params: Optional[Dict[str, Any]] = field(default_factory=dict)
    data: Optional[Union[str, bytes]] = None
    json_data: Optional[Dict[str, Any]] = None
    files: Optional[Dict[str, Any]] = None
    timeout: int = 30
    max_retries: int = 3
    verify_ssl: bool = True
    proxy: Optional[ProxyConfig] = None
    adapter: Adapter = Adapter.CURL_CFFI  # 更改默认适配器
    user_agent: str = "IPClick Client v1.0"
    follow_redirects: bool = True
    stream: bool = False

    # 新增字段用于curl_cffi
    impersonate: Optional[str] = None  # 浏览器指纹伪装

    def __post_init__(self):
        """数据验证"""
        if not self.url:
            raise ValueError("URL is required")
        if not self.url.startswith(('http://', 'https://')):
            raise ValueError("URL must start with http:// or https://")

        # 不能同时指定多种请求体
        body_fields = [self.data, self.json_data, self.files]
        if sum(x is not None for x in body_fields) > 1:
            raise ValueError("Cannot specify multiple body types (data, json_data, files)")

        # 设置默认的浏览器伪装
        if self.adapter == Adapter.CURL_CFFI and not self.impersonate:
            self.impersonate = "chrome"

    def to_protobuf(self):
        """转换为protobuf对象"""
        from ipclick.dto.proto import task_pb2

        # 转换方法枚举
        method_map = {
            HttpMethod.GET: task_pb2.GET,
            HttpMethod.POST: task_pb2.POST,
            HttpMethod.PUT: task_pb2.PUT,
            HttpMethod.DELETE: task_pb2.DELETE,
            HttpMethod.PATCH: task_pb2.PATCH,
            HttpMethod.HEAD: task_pb2.HEAD if hasattr(task_pb2, 'HEAD') else task_pb2.GET,
            HttpMethod.OPTIONS: task_pb2.OPTIONS if hasattr(task_pb2, 'OPTIONS') else task_pb2.GET,
        }

        # 转换适配器枚举
        adapter_map = {
            Adapter.CURL_CFFI: task_pb2.CURL_CFFI if hasattr(task_pb2, 'CURL_CFFI') else task_pb2.HTTPX,
            Adapter.HTTPX: task_pb2.HTTPX,
            Adapter.REQUESTS: task_pb2.REQUESTS if hasattr(task_pb2, 'REQUESTS') else task_pb2.HTTPX,
            Adapter.AIOHTTP: task_pb2.AIOHTTP if hasattr(task_pb2, 'AIOHTTP') else task_pb2.HTTPX,
        }

        # 处理代理
        proxy_info = None
        if self.proxy and self.proxy.host:
            proxy_info = task_pb2.ProxyInfo(
                scheme=self.proxy.scheme,
                host=self.proxy.host,
                port=self.proxy.port or 8080
            )

        # 处理请求体
        request_body = ""
        if self.data:
            if isinstance(self.data, bytes):
                request_body = self.data.decode('utf-8', errors='ignore')
            else:
                request_body = str(self.data)
        elif self.json_data:
            request_body = json.dumps(self.json_data, ensure_ascii=False)
        elif self.files:
            # 简单处理文件上传，实际可能需要multipart编码
            request_body = json.dumps(self.files, ensure_ascii=False)

        return task_pb2.ReqTask(
            uuid=str(uuid.uuid4()),
            adapter=adapter_map.get(self.adapter, task_pb2.HTTPX),
            url=self.url,
            method=method_map.get(self.method, task_pb2.GET),
            headers=self.headers or {},
            params=self.params or {},
            text=request_body,
            timeout_seconds=self.timeout,
            proxy=proxy_info,
            max_retries=self.max_retries,
            verify_ssl=self.verify_ssl,
            user_agent=self.user_agent
        )


@dataclass
class DownloadResponse:
    """下载响应封装"""
    request_uuid: str
    status_code: int
    headers: Dict[str, str]
    content: bytes
    text: str
    url: str
    elapsed_ms: int
    error: Optional[str] = None

    @classmethod
    def from_protobuf(cls, pb_response):
        """从protobuf响应创建对象"""
        try:
            text = pb_response.content.decode('utf-8', errors='ignore')
        except (UnicodeDecodeError, AttributeError):
            text = str(pb_response.content)

        return cls(
            request_uuid=pb_response.request_uuid,
            status_code=pb_response.status_code,
            headers=dict(pb_response.response_headers),
            content=pb_response.content,
            text=text,
            url=pb_response.effective_url,
            elapsed_ms=pb_response.response_time_ms,
            error=pb_response.error_message if pb_response.error_message else None
        )

    @classmethod
    def from_response(cls, response, request_uuid: str = ""):
        """从统一Response对象创建"""
        from ..dto.response import Response

        if isinstance(response, Response):
            return cls(
                request_uuid=request_uuid,
                status_code=response.status_code,
                headers=response.headers or {},
                content=response.content or b'',
                text=response.text or '',
                url=response.url,
                elapsed_ms=response.elapsed_ms,
                error=str(response.exception) if response.exception else None
            )
        else:
            raise ValueError("Expected Response object")

    def json(self) -> Dict[str, Any]:
        """解析JSON响应"""
        try:
            return json.loads(self.text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Response is not valid JSON: {e}")

    def is_success(self) -> bool:
        """判断请求是否成功"""
        return 200 <= self.status_code < 300 and not self.error

    def raise_for_status(self):
        """如果状态码表示错误，抛出异常"""
        if not self.is_success():
            error_msg = self.error or f"HTTP {self.status_code} Error"
            raise Exception(f"Request failed:  {error_msg}")


# 便捷的构造函数
def create_get_task(url: str, **kwargs) -> DownloadTask:
    """创建GET请求任务"""
    return DownloadTask(url=url, method=HttpMethod.GET, **kwargs)


def create_post_task(url: str, data=None, json_data=None, **kwargs) -> DownloadTask:
    """创建POST请求任务"""
    return DownloadTask(url=url, method=HttpMethod.POST, data=data, json_data=json_data, **kwargs)


def create_curl_cffi_task(url: str, impersonate: str = "chrome", **kwargs) -> DownloadTask:
    """创建curl_cffi任务"""
    return DownloadTask(url=url, adapter=Adapter.CURL_CFFI, impersonate=impersonate, **kwargs)
