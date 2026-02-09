from dataclasses import dataclass, field
from enum import Enum
import json
from typing import Any, Self

import uuid_utils as uuid

from ipclick.dto.proto import task_pb2
from ipclick.utils import json_serializer


class IPClickAdapter(Enum):
    # 定义格式: (Protobuf枚举值, 内部识别名称)
    CURL_CFFI = (task_pb2.CURL_CFFI, "curl_cffi")
    HTTPX = (task_pb2.HTTPX, "httpx")
    REQUESTS = (task_pb2.REQUESTS, "requests")
    DRISSIONPAGE = (task_pb2.DRISSIONPAGE, "DrissionPage")
    UC = (task_pb2.UC, "undetected_chromedriver")
    PLAYWRIGHT = (task_pb2.PLAYWRIGHT, "playwright")

    def __init__(self, pb_value: int, display_name: str):
        self.pb_value = pb_value
        self.display_name = display_name

    @classmethod
    def from_pb(cls, value: int) -> Self:
        """从 Protobuf 的整型枚举值找回 Enum 成员"""
        for member in cls:
            if member.pb_value == value:
                return member
        # 默认返回或抛异常
        return cls.CURL_CFFI

    @classmethod
    def from_str(cls, name: str) -> Self:
        """从字符串找回 Enum 成员 (用于 SDK 参数输入等)"""
        for member in cls:
            if member.display_name.lower() == name.lower():
                return member
        return cls.CURL_CFFI


class HttpMethod(Enum):
    GET = task_pb2.GET
    POST = task_pb2.POST
    PUT = task_pb2.PUT
    DELETE = task_pb2.DELETE
    PATCH = task_pb2.PATCH
    HEAD = task_pb2.HEAD
    OPTIONS = task_pb2.OPTIONS
    TRACE = task_pb2.TRACE


# 转换HTTP方法枚举
METHOD_MAP = {
    0: "GET",
    1: "POST",
    2: "PUT",
    3: "DELETE",
    4: "PATCH",
    5: "HEAD",
    6: "OPTIONS",
    7: "TRACE",
}


@dataclass
class ProxyConfig:
    """代理配置"""

    scheme: str = "http"
    host: str | None = None
    port: int | None = None
    auth_key: str | None = None
    auth_password: str | None = None
    channel_name: str | None = None
    session_ttl: int | None = None
    country_code: str | None = None
    tunnel_server: str | None = None

    def to_url(self) -> str | None:
        """转换为代理URL"""
        if not self.host:
            return None

        # 认证信息
        auth = f"{self.auth_key}:{self.auth_password}" if self.auth_key else ""
        # 自定义通道名
        channel_name = f":C{self.channel_name}" if self.channel_name else ""
        # 存活时间
        session_ttl = f":T{self.session_ttl}" if self.session_ttl else ""
        # 国家编码
        country_code = f":A{self.country_code}" if self.country_code else ""
        # 隧道服务器
        tunnel_server = self.tunnel_server if self.tunnel_server else f"{self.host}:{self.port}"
        # 分隔符
        delimiter = "@" if any([auth, channel_name, country_code, session_ttl]) else ""

        # curl -x {authkey}:{authpwd}:C{自定义通道名}:T{存活时间}:A{国家编码}@{隧道服务器} {目标url}
        return f"{self.scheme}://{auth}{channel_name}{session_ttl}{country_code}{delimiter}{tunnel_server}"


@dataclass
class DownloadTask:
    """下载任务"""

    uuid: str = ""
    adapter: IPClickAdapter = IPClickAdapter.CURL_CFFI

    # 协议
    method: HttpMethod = HttpMethod.GET
    url: str = ""
    headers: dict[str, Any] | None = None
    cookies: dict[str, Any] | str | None = None
    params: dict[str, Any] | None = None
    data: Any = None
    json: dict[str, Any] | None = None
    files: dict[str, Any] | None = None
    proxy: ProxyConfig | str | bool | None = None
    timeout: float = 60
    max_retries: int = 3
    retry_backoff: float = 2.0
    verify: bool = True
    allow_redirects: bool = True
    stream: bool = False

    impersonate: str | None = None  # curl_cffi 浏览器指纹伪装
    extensions: dict[str, Any] | None = field(default_factory=dict)  # 拓展字段

    # 渲染
    automation_config: str | None = None
    automation_script: str | None = None

    allowed_status_codes: list[int] = field(default_factory=list)  # 允许的状态码

    # kwargs
    kwargs: str | None = None

    def __post_init__(self):
        """数据验证"""
        if not self.url:
            raise ValueError("URL is required")
        if not self.url.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        if self.files:
            raise NotImplementedError("files is not supported.")

        # 不能同时指定多种请求体
        body_fields = [self.data, self.json, self.files]
        if sum(x is not None for x in body_fields) > 1:
            raise ValueError("Cannot specify multiple body types (data, json_data, files)")

        # 设置默认的浏览器伪装
        if self.adapter == IPClickAdapter.CURL_CFFI and not self.impersonate:
            self.impersonate = "chrome"

        if not self.allowed_status_codes:
            self.allowed_status_codes = [200, 404]

    def to_protobuf(self):
        """转换为protobuf对象"""
        try:
            if isinstance(self.adapter, str):
                adapter_member = IPClickAdapter.from_str(self.adapter)
            else:
                adapter_member = self.adapter or IPClickAdapter.CURL_CFFI

            return task_pb2.ReqTask(
                uuid=str(self.uuid) or str(uuid.uuid7()),
                adapter=adapter_member.pb_value,
                method=self.method.value,
                url=self.url,
                headers=self.headers,
                cookies=self.cookies,
                params=json.dumps(self.params, default=json_serializer) if self.params else None,
                data=json.dumps(self.data, default=json_serializer) if self.data else None,
                json=json.dumps(self.json, default=json_serializer) if self.json else None,
                proxy=self.proxy,
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
                kwargs=self.kwargs,
            )
        except Exception as e:
            raise Exception(f"转换 protobuf 失败：{e}")


@dataclass
class DownloadResponse:
    """下载响应封装"""

    request_uuid: str
    adapter_type: str
    request: Any
    url: str
    status_code: int
    headers: dict[str, str]
    content: bytes
    text: str

    elapsed_ms: int
    error: str | None = None

    @classmethod
    def from_protobuf(cls, pb_response):
        """从protobuf响应创建对象"""
        try:
            text = pb_response.content.decode("utf-8", errors="ignore")
        except (UnicodeDecodeError, AttributeError):
            text = str(pb_response.content)

        return cls(
            request_uuid=pb_response.request_uuid,
            adapter_type=pb_response.adapter,
            request=pb_response.original_request,
            url=pb_response.effective_url,
            status_code=pb_response.status_code,
            headers=dict(pb_response.response_headers),
            content=pb_response.content,
            text=text,
            error=pb_response.error_message if pb_response.error_message else None,
            elapsed_ms=pb_response.response_time_ms,
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
                content=response.content or b"",
                text=response.text or "",
                url=response.url,
                elapsed_ms=response.elapsed_ms,
                error=str(response.exception) if response.exception else None,
            )
        else:
            raise ValueError("Expected Response object")

    def json(self) -> dict[str, Any]:
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
