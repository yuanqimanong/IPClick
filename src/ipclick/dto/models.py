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
from typing import Optional, Dict, Any, List

from ipclick import load_config
from ipclick.dto.proto import task_pb2


class Adapter(Enum):
    # 协议
    CURL_CFFI = "curl_cffi"  # 默认适配器
    HTTPX = "httpx"
    REQUESTS = "requests"
    # 渲染
    DRISSIONPAGE = "DrissionPage"
    UC = "undetected_chromedriver"
    PLAYWRIGHT = "playwright"


class HttpMethod(Enum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"
    PATCH = "PATCH"
    HEAD = "HEAD"
    OPTIONS = "OPTIONS"
    TRACE = "TRACE"


# 转换适配器枚举
ADAPTER_MAP = {
    Adapter.CURL_CFFI: task_pb2.CURL_CFFI,
    Adapter.HTTPX: task_pb2.HTTPX,
    Adapter.REQUESTS: task_pb2.REQUESTS,
    Adapter.DRISSIONPAGE: task_pb2.DRISSIONPAGE,
    Adapter.UC: task_pb2.UC,
    Adapter.PLAYWRIGHT: task_pb2.PLAYWRIGHT,
}

# 转换方法枚举
METHOD_MAP = {
    HttpMethod.GET: task_pb2.GET,
    HttpMethod.POST: task_pb2.POST,
    HttpMethod.PUT: task_pb2.PUT,
    HttpMethod.DELETE: task_pb2.DELETE,
    HttpMethod.PATCH: task_pb2.PATCH,
    HttpMethod.HEAD: task_pb2.HEAD,
    HttpMethod.OPTIONS: task_pb2.OPTIONS,
    HttpMethod.TRACE: task_pb2.TRACE,
}


@dataclass
class ProxyConfig:
    """代理配置"""
    scheme: str = "http"
    host: Optional[str] = None
    port: Optional[int] = None
    auth_key: Optional[str] = None
    auth_password: Optional[str] = None
    channel_name: Optional[str] = None
    session_ttl: Optional[int] = None
    country_code: Optional[str] = None
    tunnel_server: Optional[str] = None

    def to_url(self) -> Optional[str]:
        """转换为代理URL"""
        if not self.host:
            return None

        # 认证信息
        auth = f'{self.auth_key}:{self.auth_password}' if self.auth_key else ''
        # 自定义通道名
        channel_name = f':C{self.channel_name}' if self.channel_name else ''
        # 存活时间
        session_ttl = f':T{self.session_ttl}' if self.session_ttl else ''
        # 国家编码
        country_code = f':A{self.country_code}' if self.country_code else ''
        # 隧道服务器
        tunnel_server = self.tunnel_server if self.tunnel_server else f'{self.host}:{self.port}'
        # 分隔符
        delimiter = '@' if any([auth, channel_name, country_code, session_ttl]) else ''

        # curl -x {authkey}:{authpwd}:C{自定义通道名}:T{存活时间}:A{国家编码}@{隧道服务器} {目标url}
        return f"{self.scheme}://{auth}{channel_name}{session_ttl}{country_code}{delimiter}{tunnel_server}"


@dataclass
class DownloadTask:
    """用户友好的下载任务"""
    uuid: str = ""
    adapter: Adapter = Adapter.CURL_CFFI

    # 协议
    method: HttpMethod = HttpMethod.GET
    url: str = ""
    headers: Optional[Dict[str, Any]] = None
    cookies: Optional[Dict[str, Any], str] = None
    params: Optional[Dict[str, Any]] = None
    data: Any = None
    json: Optional[Dict[str, Any]] = None
    files: Optional[Dict[str, Any]] = None
    proxy: Optional[ProxyConfig, str, bool] = None
    timeout: float = 60
    max_retries: int = 3
    retry_backoff: float = 2.0
    verify: bool = True
    allow_redirects: bool = True
    stream: bool = False

    impersonate: Optional[str] = None  # curl_cffi 浏览器指纹伪装
    extensions: Optional[Dict[str, Any]] = field(default_factory=dict)  # 拓展字段

    # 渲染
    automation_config: str = None
    automation_script: str = None

    allowed_status_codes: List[int] = field(default_factory=list)  # 允许的状态码

    # kwargs
    kwargs: str = None

    def __post_init__(self):
        """数据验证"""
        if not self.url:
            raise ValueError("URL is required")
        if not self.url.startswith(('http://', 'https://')):
            raise ValueError("URL must start with http:// or https://")
        if self.files:
            raise NotImplementedError("files is not supported.")

        # 不能同时指定多种请求体
        body_fields = [self.data, self.json, self.files]
        if sum(x is not None for x in body_fields) > 1:
            raise ValueError("Cannot specify multiple body types (data, json_data, files)")

        # 设置默认的浏览器伪装
        if self.adapter == Adapter.CURL_CFFI and not self.impersonate:
            self.impersonate = "chrome"

        if not self.allowed_status_codes:
            self.allowed_status_codes = [200, 404]

    def to_protobuf(self):
        """转换为protobuf对象"""

        # 处理代理
        if self.proxy is True:
            config = load_config()
            proxy = ProxyConfig(**config.get("PROXY", {})).to_url()
        elif isinstance(self.proxy, ProxyConfig):
            proxy = self.proxy.to_url()
        else:
            proxy = self.proxy

        return task_pb2.ReqTask(
            uuid=str(self.uuid) or str(uuid.uuid4()),
            adapter=ADAPTER_MAP.get(self.adapter, task_pb2.CURL_CFFI),
            method=METHOD_MAP.get(self.method, task_pb2.GET),
            url=self.url,
            headers=self.headers,
            cookies=self.cookies,
            params=self.params,
            data=json.dumps(self.data) if self.data else None,
            json=json.dumps(self.json) if self.json else None,
            proxy=proxy,
            timeout_seconds=self.timeout,
            max_retries=self.max_retries,
            retry_backoff_seconds=self.retry_backoff,
            verify_ssl=self.verify,
            allow_redirects=self.allow_redirects,
            stream=self.stream,
            impersonate=self.impersonate,
            extensions=self.extensions,
            automation_config=self.automation_config,
            automation_script=self.automation_script,
            allowed_status_codes=self.allowed_status_codes,
            kwargs=self.kwargs
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
