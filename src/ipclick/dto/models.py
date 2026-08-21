"""定义 SDK 与 gRPC 边界之间转换的请求和响应数据模型。"""

from dataclasses import dataclass, field
from enum import Enum
import json
import math
from typing import Any, Self, cast
from urllib.parse import quote

import uuid_utils as uuid

from ipclick.adapters.settings import HARD_MAX_RETRIES
from ipclick.dto.proto import task_pb2
from ipclick.dto.response import decode_content
from ipclick.exceptions import RequestError, ValidationError
from ipclick.utils import json_serializer


class IPClickAdapter(Enum):
    """适配器的 Protobuf 枚举值与用户可见名称。"""

    CURL_CFFI = (task_pb2.CURL_CFFI, "curl_cffi")
    HTTPX = (task_pb2.HTTPX, "httpx")
    REQUESTS = (task_pb2.REQUESTS, "requests")
    NIQUESTS = (task_pb2.NIQUESTS, "niquests")
    DRISSIONPAGE = (task_pb2.DRISSIONPAGE, "DrissionPage")
    UC = (task_pb2.UC, "undetected_chromedriver")
    PLAYWRIGHT = (task_pb2.PLAYWRIGHT, "playwright")
    CAMOUFOX = (task_pb2.CAMOUFOX, "camoufox")
    PATCHRIGHT = (task_pb2.PATCHRIGHT, "patchright")
    BROWSER = (task_pb2.BROWSER, "browser")

    def __init__(self, pb_value: int, display_name: str):
        """保存线上的枚举值及稳定的配置名称。"""
        self.pb_value = pb_value
        self.display_name = display_name

    @classmethod
    def from_pb(cls, value: int) -> Self:
        """从 Protobuf 枚举值解析适配器。"""
        for member in cls:
            if member.pb_value == value:
                return member
        raise ValueError(f"未知的适配器枚举值: {value}")

    @classmethod
    def from_str(cls, name: str) -> Self:
        """不区分大小写地从配置名称解析适配器。"""
        for member in cls:
            if member.display_name.lower() == name.lower():
                return member
        supported = ", ".join(m.display_name for m in cls)
        raise ValueError(f"未知的适配器名称: {name!r}，可选值: {supported}")


class HttpMethod(Enum):
    """IPClick 支持的 HTTP 方法及其 Protobuf 枚举值。"""

    GET = task_pb2.GET
    POST = task_pb2.POST
    PUT = task_pb2.PUT
    DELETE = task_pb2.DELETE
    PATCH = task_pb2.PATCH
    HEAD = task_pb2.HEAD
    OPTIONS = task_pb2.OPTIONS
    TRACE = task_pb2.TRACE


METHOD_MAP: dict[int, str] = {member.value: member.name for member in HttpMethod}


@dataclass
class ProxyConfig:
    """代理连接与供应商会话参数。"""

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
        """把结构化代理参数编码为适配器接受的代理 URL。"""
        if not self.host and not self.tunnel_server:
            return None

        auth = (
            f"{quote(str(self.auth_key), safe='')}:{quote(str(self.auth_password or ''), safe='')}"
            if self.auth_key
            else ""
        )
        channel_name = f":C{quote(str(self.channel_name), safe='')}" if self.channel_name else ""
        session_ttl = f":T{quote(str(self.session_ttl), safe='')}" if self.session_ttl else ""
        country_code = f":A{quote(str(self.country_code), safe='')}" if self.country_code else ""
        if self.tunnel_server:
            tunnel_server = self.tunnel_server
        else:
            if self.port is None or isinstance(self.port, bool) or not 1 <= self.port <= 65535:
                raise ValidationError("proxy port must be between 1 and 65535 when host is configured")
            host = str(self.host).strip()
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            tunnel_server = f"{host}:{self.port}"
        delimiter = "@" if any([auth, channel_name, country_code, session_ttl]) else ""

        return f"{self.scheme}://{auth}{channel_name}{session_ttl}{country_code}{delimiter}{tunnel_server}"


@dataclass
class DownloadTask:
    """一次下载请求及其重试、代理、渲染和流式选项。"""

    uuid: str = ""
    adapter: IPClickAdapter | str = IPClickAdapter.CURL_CFFI

    method: HttpMethod = HttpMethod.GET
    url: str = ""
    headers: dict[str, Any] | None = None
    cookies: dict[str, Any] | str | None = None
    params: dict[str, Any] | None = None
    data: Any = None
    json: dict[str, Any] | None = None
    proxy: ProxyConfig | str | bool | None = None
    timeout: float = 60
    max_retries: int = 3
    retry_backoff: float = 2.0
    verify: bool = True
    allow_redirects: bool = True
    stream: bool = False

    impersonate: str | None = None

    automation_config: str | None = None
    automation_script: str | None = None

    allowed_status_codes: list[int] = field(default_factory=list)

    kwargs: str | None = None

    def __post_init__(self) -> None:
        """校验跨适配器都必须满足的请求约束并填充默认值。"""
        if not self.url:
            raise ValidationError("URL is required")
        if not self.url.startswith(("http://", "https://")):
            raise ValidationError("URL must start with http:// or https://")

        if self.data is not None and self.json is not None:
            raise ValidationError("Cannot specify both data and json")

        # 数据类类型提示不构成运行时边界；外部调用仍可能传入动态 JSON 值。
        max_retries = cast(Any, self.max_retries)
        timeout = cast(Any, self.timeout)
        retry_backoff = cast(Any, self.retry_backoff)
        if (
            isinstance(max_retries, bool)
            or not isinstance(max_retries, int)
            or not 0 <= max_retries <= HARD_MAX_RETRIES
        ):
            raise ValidationError(f"max_retries must be between 0 and {HARD_MAX_RETRIES}")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValidationError("timeout must be > 0")
        if (
            isinstance(retry_backoff, bool)
            or not isinstance(retry_backoff, (int, float))
            or not math.isfinite(retry_backoff)
            or retry_backoff < 0
        ):
            raise ValidationError("retry_backoff must be a finite number >= 0")

        if self.adapter == IPClickAdapter.CURL_CFFI and not self.impersonate:
            self.impersonate = "chrome"

        if not self.allowed_status_codes:
            self.allowed_status_codes = [200, 404]

    @staticmethod
    def _stringify_map(mapping: dict[str, Any] | None) -> dict[str, str] | None:
        """将 headers/cookies 中的非字符串值稳定编码为字符串。"""
        if not mapping:
            return None
        return {str(k): v if isinstance(v, str) else json.dumps(v, default=json_serializer) for k, v in mapping.items()}

    @staticmethod
    def _encode_body(body: Any) -> bytes | None:
        """把请求体编码为 Protobuf bytes 字段。"""
        if body is None:
            return None
        if isinstance(body, (bytes, bytearray, memoryview)):
            return bytes(body)
        if isinstance(body, str):
            return body.encode("utf-8")
        return json.dumps(body, default=json_serializer).encode("utf-8")

    def _data_is_raw(self) -> bool | None:
        """请求体是否是原始字节/文本——决定服务端要不要把它还原成结构化对象。

        str/bytes 一律原样发出：``data='{"a": 1}'`` 是一段 JSON 文本，不是表单。
        dict/list 走 JSON 序列化过线，服务端再还原回来交给适配器编码成表单。
        """
        if self.data is None:
            return None
        return isinstance(self.data, (str, bytes, bytearray, memoryview))

    @staticmethod
    def _cookies_to_map(cookies: dict[str, Any] | str | None) -> dict[str, str] | None:
        """兼容字典或 Cookie 请求头形式，并归一为键值映射。"""
        if not cookies:
            return None
        if isinstance(cookies, str):
            parsed: dict[str, str] = {}
            for item in cookies.split(";"):
                name, sep, value = item.partition("=")
                if sep:
                    parsed[name.strip()] = value.strip()
            return parsed or None
        return DownloadTask._stringify_map(cookies)

    def _proxy_to_str(self) -> str | None:
        """将代理选项归一为 URL；``True`` 必须先由客户端解析。"""
        if self.proxy is None or self.proxy is False:
            return None
        if self.proxy is True:
            raise ValidationError("proxy=True 需要由 Downloader 解析配置文件后再构造 DownloadTask")
        if isinstance(self.proxy, ProxyConfig):
            return self.proxy.to_url()
        return str(self.proxy)

    def to_protobuf(self) -> "task_pb2.ReqTask":
        """将已校验的请求转换为 gRPC 传输消息。"""
        try:
            if isinstance(self.adapter, str):
                adapter_member = IPClickAdapter.from_str(self.adapter)
            else:
                adapter_member = self.adapter or IPClickAdapter.CURL_CFFI

            return task_pb2.ReqTask(
                uuid=str(self.uuid) or str(uuid.uuid7()),
                adapter=cast("task_pb2.AdapterType", adapter_member.pb_value),
                method=self.method.value,
                url=self.url,
                headers=self._stringify_map(self.headers),
                cookies=self._cookies_to_map(self.cookies),
                params=json.dumps(self.params, default=json_serializer) if self.params else None,
                data=self._encode_body(self.data),
                data_is_raw=self._data_is_raw(),
                json=json.dumps(self.json, default=json_serializer) if self.json is not None else None,
                proxy=self._proxy_to_str(),
                timeout_seconds=self.timeout,
                max_retries=self.max_retries,
                retry_backoff_seconds=self.retry_backoff,
                verify_ssl=self.verify,
                allow_redirects=self.allow_redirects,
                stream=self.stream,
                impersonate=self.impersonate,
                automation_config=self.automation_config,
                automation_script=self.automation_script,
                allowed_status_codes=self.allowed_status_codes,
                kwargs=self.kwargs,
            )
        except ValidationError:
            raise
        except Exception as e:
            raise ValidationError(f"转换 protobuf 失败：{e}") from e


@dataclass
class ResponseTrace:
    """随响应返回的执行节点、适配器与排队信息。"""

    node_id: str = ""
    adapter: str = ""
    attempts: int = 1
    forwarded: bool = False
    queued_ms: int = 0

    @classmethod
    def from_protobuf(cls, pb_trace: "task_pb2.Trace") -> "ResponseTrace":
        """从 Protobuf trace 构造 SDK 跟踪信息。"""
        return cls(
            node_id=pb_trace.node_id,
            adapter=pb_trace.adapter,
            attempts=pb_trace.attempts or 1,
            forwarded=pb_trace.forwarded,
            queued_ms=pb_trace.queued_ms,
        )


@dataclass
class DownloadResponse:
    """SDK 对外返回的统一下载结果；网络失败以 ``status_code=-1`` 表示。"""

    request_uuid: str = ""
    adapter_type: str = ""
    request: Any = None
    url: str = ""
    status_code: int = -1
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""
    text: str = ""

    elapsed_ms: int = 0
    error: str | None = None
    trace: ResponseTrace = field(default_factory=lambda: ResponseTrace())

    @staticmethod
    def _adapter_name(pb_value: int) -> str:
        """解析适配器名称，并兼容比当前客户端更新的枚举值。"""
        try:
            return IPClickAdapter.from_pb(pb_value).display_name
        except ValueError:
            return str(pb_value)

    @classmethod
    def from_protobuf(cls, pb_response: "task_pb2.TaskResp") -> "DownloadResponse":
        """从服务端响应消息构建用户侧结果。"""
        headers = dict(pb_response.response_headers)
        text = decode_content(pb_response.content, headers)

        return cls(
            request_uuid=pb_response.request_uuid,
            adapter_type=cls._adapter_name(pb_response.adapter),
            url=pb_response.effective_url,
            status_code=pb_response.status_code,
            headers=headers,
            content=pb_response.content,
            text=text,
            error=pb_response.error_message if pb_response.error_message else None,
            elapsed_ms=pb_response.response_time_ms,
            trace=ResponseTrace.from_protobuf(pb_response.trace) if pb_response.HasField("trace") else ResponseTrace(),
        )

    @classmethod
    def from_response(cls, response: Any, request_uuid: str = "", adapter_type: str = "") -> "DownloadResponse":
        """将本地适配器 ``Response`` 转为 SDK 统一响应。"""
        from ipclick.dto.response import Response

        if not isinstance(response, Response):
            raise TypeError(f"Expected Response object, got {type(response).__name__}")

        return cls(
            request_uuid=request_uuid,
            adapter_type=adapter_type,
            request=None,
            status_code=response.status_code,
            headers=response.headers or {},
            content=response.content or b"",
            text=response.text or "",
            url=response.url,
            elapsed_ms=response.elapsed_ms,
            error=str(response.exception) if response.exception else None,
        )

    @classmethod
    def from_error(
        cls, error: str, url: str = "", request_uuid: str = "", adapter_type: str = ""
    ) -> "DownloadResponse":
        """构造未取得 HTTP 响应的失败结果。"""
        return cls(
            request_uuid=request_uuid,
            adapter_type=adapter_type,
            url=url,
            status_code=-1,
            error=error,
        )

    def json(self) -> Any:
        """解析文本响应体；内容不是 JSON 时抛出 ``ValueError``。"""
        try:
            return json.loads(self.text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Response is not valid JSON: {e}") from e

    def is_success(self) -> bool:
        """判断响应是否为无错误的 2xx。"""
        return 200 <= self.status_code < 300 and not self.error

    @property
    def ok(self) -> bool:
        """提供与常见 HTTP 客户端一致的成功状态属性。"""
        return self.is_success()

    def raise_for_status(self) -> None:
        """在 HTTP 非 2xx 或传输失败时抛出 ``RequestError``。"""
        if not self.is_success():
            error_msg = self.error or f"HTTP {self.status_code} Error"
            raise RequestError(f"Request failed: {error_msg}")
