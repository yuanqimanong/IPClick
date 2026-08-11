from dataclasses import dataclass, field
from enum import Enum
import json
from typing import Any, Self

import uuid_utils as uuid

from ipclick.dto.proto import task_pb2
from ipclick.exceptions import RequestError, ValidationError
from ipclick.utils import json_serializer


class IPClickAdapter(Enum):
    # 定义格式: (Protobuf枚举值, 内部识别名称)
    CURL_CFFI = (task_pb2.CURL_CFFI, "curl_cffi")
    HTTPX = (task_pb2.HTTPX, "httpx")
    #: 已移除的适配器。枚举值保留是为了兼容旧客户端——它们发来这个值时
    #: 服务端会明确报错，而不是静默换成别的适配器。
    REQUESTS = (task_pb2.REQUESTS, "requests")
    NIQUESTS = (task_pb2.NIQUESTS, "niquests")
    DRISSIONPAGE = (task_pb2.DRISSIONPAGE, "DrissionPage")
    UC = (task_pb2.UC, "undetected_chromedriver")
    PLAYWRIGHT = (task_pb2.PLAYWRIGHT, "playwright")
    CAMOUFOX = (task_pb2.CAMOUFOX, "camoufox")
    PATCHRIGHT = (task_pb2.PATCHRIGHT, "patchright")
    #: "用浏览器渲染就行，引擎由服务端定"。服务端按 [BROWSER].engine 解析，
    #: 默认按平台选：Windows -> DrissionPage，Linux/macOS -> Camoufox。
    #: 客户端不必关心服务端装了哪个内核。
    BROWSER = (task_pb2.BROWSER, "browser")

    def __init__(self, pb_value: int, display_name: str):
        self.pb_value = pb_value
        self.display_name = display_name

    @classmethod
    def from_pb(cls, value: int) -> Self:
        """从 Protobuf 的整型枚举值找回 Enum 成员

        Raises:
            ValueError: 枚举值未知。静默回退到 CURL_CFFI 会让调用方以为用了
                自己指定的适配器，实际却换了一个，因此这里直接报错。
        """
        for member in cls:
            if member.pb_value == value:
                return member
        raise ValueError(f"未知的适配器枚举值: {value}")

    @classmethod
    def from_str(cls, name: str) -> Self:
        """从字符串找回 Enum 成员 (用于 SDK 参数输入等)

        Raises:
            ValueError: 名称未知（含拼写错误）。
        """
        for member in cls:
            if member.display_name.lower() == name.lower():
                return member
        supported = ", ".join(m.display_name for m in cls)
        raise ValueError(f"未知的适配器名称: {name!r}，可选值: {supported}")


class HttpMethod(Enum):
    GET = task_pb2.GET
    POST = task_pb2.POST
    PUT = task_pb2.PUT
    DELETE = task_pb2.DELETE
    PATCH = task_pb2.PATCH
    HEAD = task_pb2.HEAD
    OPTIONS = task_pb2.OPTIONS
    TRACE = task_pb2.TRACE


# 转换HTTP方法枚举（由 HttpMethod 推导，避免与 proto 枚举各写一份而失步）
METHOD_MAP: dict[int, str] = {member.value: member.name for member in HttpMethod}


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
        """转换为代理URL

        Returns:
            代理 URL；当既没有 host 也没有 tunnel_server 时返回 None（表示不走代理）。
        """
        if not self.host and not self.tunnel_server:
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
    # 允许传适配器名字符串（如 "httpx"），to_protobuf() 会解析成枚举成员
    adapter: IPClickAdapter | str = IPClickAdapter.CURL_CFFI

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
            raise ValidationError("URL is required")
        if not self.url.startswith(("http://", "https://")):
            raise ValidationError("URL must start with http:// or https://")
        if self.files:
            raise NotImplementedError("files is not supported.")

        # 不能同时指定多种请求体
        body_fields = [self.data, self.json, self.files]
        if sum(x is not None for x in body_fields) > 1:
            raise ValidationError("Cannot specify multiple body types (data, json, files)")

        if self.max_retries < 0:
            raise ValidationError("max_retries must be >= 0")
        if self.timeout <= 0:
            raise ValidationError("timeout must be > 0")

        # 设置默认的浏览器伪装
        if self.adapter == IPClickAdapter.CURL_CFFI and not self.impersonate:
            self.impersonate = "chrome"

        if not self.allowed_status_codes:
            self.allowed_status_codes = [200, 404]

    @staticmethod
    def _stringify_map(mapping: dict[str, Any] | None) -> dict[str, str] | None:
        """protobuf 的 map<string, string> 只接受字符串值，这里统一转换。"""
        if not mapping:
            return None
        return {str(k): v if isinstance(v, str) else json.dumps(v, default=json_serializer) for k, v in mapping.items()}

    @staticmethod
    def _cookies_to_map(cookies: dict[str, Any] | str | None) -> dict[str, str] | None:
        """把 dict 或 ``"a=1; b=2"`` 形式的 cookie 串统一成 map<string, string>。"""
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
        """把 ProxyConfig / str / bool 三种代理写法归一成 URL 字符串。

        ``proxy=True`` 只在 SDK 层有意义（表示"用配置文件里的代理"），到了这里
        已经应该被解析成具体配置；若仍是 True 说明调用方直接构造了 DownloadTask，
        此时无从得知代理地址，明确报错好过静默不走代理。
        """
        if self.proxy is None or self.proxy is False:
            return None
        if self.proxy is True:
            raise ValidationError("proxy=True 需要由 Downloader 解析配置文件后再构造 DownloadTask")
        if isinstance(self.proxy, ProxyConfig):
            return self.proxy.to_url()
        return str(self.proxy)

    def to_protobuf(self) -> "task_pb2.ReqTask":
        """转换为protobuf对象"""
        try:
            if isinstance(self.adapter, str):
                adapter_member = IPClickAdapter.from_str(self.adapter)
            else:
                adapter_member = self.adapter or IPClickAdapter.CURL_CFFI

            return task_pb2.ReqTask(
                uuid=str(self.uuid) or str(uuid.uuid7()),
                # protobuf 生成的 stub 把枚举参数标成 AdapterType，运行时接受 int
                adapter=adapter_member.pb_value,  # pyright: ignore[reportArgumentType]
                method=self.method.value,
                url=self.url,
                headers=self._stringify_map(self.headers),
                cookies=self._cookies_to_map(self.cookies),
                params=json.dumps(self.params, default=json_serializer) if self.params else None,
                data=json.dumps(self.data, default=json_serializer) if self.data is not None else None,
                json=json.dumps(self.json, default=json_serializer) if self.json is not None else None,
                proxy=self._proxy_to_str(),
                # 这些字段在 proto 里是 optional，显式传值即代表"已设置"，
                # 服务端才能把"未设置"和"显式设为 0/false"区分开。
                timeout_seconds=self.timeout,
                max_retries=self.max_retries,
                retry_backoff_seconds=self.retry_backoff,
                verify_ssl=self.verify,
                allow_redirects=self.allow_redirects,
                stream=self.stream,
                impersonate=self.impersonate,
                extensions=self._stringify_map(self.extensions),
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
class DownloadResponse:
    """下载响应封装"""

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

    @staticmethod
    def _adapter_name(pb_value: int) -> str:
        """protobuf 传来的是枚举整数，对外统一暴露适配器名称。"""
        try:
            return IPClickAdapter.from_pb(pb_value).display_name
        except ValueError:
            return str(pb_value)

    @classmethod
    def from_protobuf(cls, pb_response: "task_pb2.TaskResp") -> "DownloadResponse":
        """从protobuf响应创建对象"""
        try:
            text = pb_response.content.decode("utf-8", errors="ignore")
        except (UnicodeDecodeError, AttributeError):
            text = str(pb_response.content)

        return cls(
            request_uuid=pb_response.request_uuid,
            adapter_type=cls._adapter_name(pb_response.adapter),
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
    def from_response(cls, response: Any, request_uuid: str = "", adapter_type: str = "") -> "DownloadResponse":
        """从统一Response对象创建"""
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
        """构造一个表示本地失败（如连不上服务端）的响应。

        状态码 -1 与适配器侧的 ``Response.error_response`` 保持一致。
        """
        return cls(
            request_uuid=request_uuid,
            adapter_type=adapter_type,
            url=url,
            status_code=-1,
            error=error,
        )

    def json(self) -> Any:
        """解析JSON响应"""
        try:
            return json.loads(self.text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Response is not valid JSON: {e}") from e

    def is_success(self) -> bool:
        """判断请求是否成功"""
        return 200 <= self.status_code < 300 and not self.error

    @property
    def ok(self) -> bool:
        """``is_success()`` 的属性别名，与 ``dto.response.Response.ok`` 对齐。"""
        return self.is_success()

    def raise_for_status(self) -> None:
        """如果状态码表示错误，抛出异常

        Raises:
            RequestError: 请求失败。注意必须抛子类而不是基类 IPClickError——
                README 文档写的是 RequestError，而基类实例并不会被
                ``except RequestError:`` 捕获。
        """
        if not self.is_success():
            error_msg = self.error or f"HTTP {self.status_code} Error"
            raise RequestError(f"Request failed: {error_msg}")
