"""适配器内部使用的轻量 HTTP 响应对象。"""

from collections.abc import Mapping
from dataclasses import dataclass
import json as json_module
from typing import Any

from typing_extensions import override

from ipclick.exceptions import RequestError


def content_type_from_headers(headers: Mapping[str, str] | None) -> str | None:
    """按 HTTP 头字段不区分大小写的规则读取 ``Content-Type``。"""
    return next((value for name, value in (headers or {}).items() if name.lower() == "content-type"), None)


def encoding_from_headers(headers: Mapping[str, str] | None) -> str:
    """从 ``Content-Type`` 参数读取字符集，缺省为 UTF-8。"""
    content_type = content_type_from_headers(headers)
    if content_type:
        # MIME 参数名不区分大小写，值也常被引号包裹。
        for parameter in content_type.split(";")[1:]:
            name, separator, value = parameter.partition("=")
            if separator and name.strip().lower() == "charset":
                encoding = value.strip().strip("\"'")
                if encoding:
                    return encoding
    return "utf-8"


def decode_content(content: bytes, headers: Mapping[str, str] | None = None) -> str:
    """按响应头字符集解码正文，未知编码时安全回退到 UTF-8。"""
    try:
        return content.decode(encoding_from_headers(headers), errors="replace")
    except LookupError:
        return content.decode("utf-8", errors="replace")


@dataclass
class Response:
    """归一化不同 HTTP/浏览器适配器的结果与异常。"""

    url: str
    status_code: int
    content: bytes | None = None
    text: str | None = None
    headers: dict[str, str] | None = None
    raw_response: Any | None = None
    exception: Exception | None = None
    elapsed_ms: int = 0
    attempts: int = 1

    def __post_init__(self) -> None:
        """按声明字符集补齐可直接消费的文本和响应头默认值。"""
        if self.text is None and self.content is not None:
            try:
                self.text = decode_content(self.content, self.headers)
            except AttributeError:
                self.text = str(self.content)

        if self.headers is None:
            self.headers = {}

    @property
    def ok(self) -> bool:
        """响应是否为无适配器异常的 2xx。"""
        return 200 <= self.status_code < 300 and self.exception is None

    @property
    def is_success(self) -> bool:
        """``ok`` 的语义别名。"""
        return self.ok

    @property
    def is_redirect(self) -> bool:
        """响应是否属于 3xx 重定向。"""
        return 300 <= self.status_code < 400

    @property
    def is_client_error(self) -> bool:
        """响应是否属于 4xx 客户端错误。"""
        return 400 <= self.status_code < 500

    @property
    def is_server_error(self) -> bool:
        """响应是否属于 5xx 服务端错误。"""
        return 500 <= self.status_code < 600

    def json(self) -> Any:
        """解析文本响应体，空内容或非法 JSON 会抛出 ``ValueError``。"""
        if not self.text:
            raise ValueError("Response has no text content")

        try:
            return json_module.loads(self.text)
        except json_module.JSONDecodeError as e:
            raise ValueError(f"Response is not valid JSON: {e}") from e

    def raise_for_status(self) -> None:
        """重新抛出适配器异常，或为非 2xx 构造 ``RequestError``。"""
        if self.exception:
            raise self.exception

        if not self.ok:
            error_msg = f"HTTP {self.status_code} Error for url: {self.url}"
            if self.text:
                error_msg += f"\nResponse: {self.text[:200]}"
            raise RequestError(error_msg)

    def get_content_type(self) -> str | None:
        """按 HTTP 头字段不区分大小写的规则读取 ``Content-Type``。"""
        return content_type_from_headers(self.headers)

    def get_encoding(self) -> str:
        """从 ``Content-Type`` 参数读取字符集，缺省为 UTF-8。"""
        return encoding_from_headers(self.headers)

    @classmethod
    def error_response(cls, url: str, exception: Exception, status_code: int = -1, attempts: int = 1) -> "Response":
        """构造保留原异常及尝试次数的失败响应。"""
        return cls(
            url=url,
            status_code=status_code,
            content=None,
            text=str(exception),
            headers={},
            raw_response=None,
            exception=exception,
            attempts=attempts,
        )

    @classmethod
    def success_response(
        cls, url: str, content: bytes = b"", status_code: int = 200, headers: dict[str, str] | None = None
    ) -> "Response":
        """构造不依赖第三方原始响应对象的成功响应。"""
        return cls(
            url=url,
            status_code=status_code,
            content=content,
            text=None,
            headers=headers or {},
            raw_response=None,
            exception=None,
        )

    def to_dict(self) -> dict[str, Any]:
        """返回适合日志与结构化输出的响应摘要。"""
        return {
            "url": self.url,
            "status_code": self.status_code,
            "headers": self.headers,
            "text": self.text,
            "elapsed_ms": self.elapsed_ms,
            "attempts": self.attempts,
            "ok": self.ok,
            "exception": str(self.exception) if self.exception else None,
        }

    @override
    def __str__(self) -> str:
        """返回类似常见 HTTP 客户端的精简状态文本。"""
        return f"<Response [{self.status_code}] {self.url}>"

    @override
    def __repr__(self) -> str:
        """返回包含诊断字段的开发者表示。"""
        return f"Response(url={self.url!r}, status_code={self.status_code}, elapsed_ms={self.elapsed_ms}, ok={self.ok})"
