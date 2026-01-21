from dataclasses import dataclass
import json as json_module
from typing import Any, Dict, Optional


@dataclass
class Response:
    """
    统一的HTTP响应类

    用于封装所有HTTP适配器的响应，提供一致的接口
    """

    url: str
    status_code: int
    content: Optional[bytes] = None
    text: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    raw_response: Optional[Any] = None
    exception: Optional[Exception] = None
    elapsed_ms: int = 0

    def __post_init__(self):
        """初始化后处理"""
        # 如果没有text但有content，尝试解码
        if self.text is None and self.content is not None:
            try:
                self.text = self.content.decode("utf-8", errors="ignore")
            except (AttributeError, UnicodeDecodeError):
                self.text = str(self.content)

        # 确保headers是字典
        if self.headers is None:
            self.headers = {}

    @property
    def ok(self) -> bool:
        """判断响应是否成功"""
        return 200 <= self.status_code < 300 and self.exception is None

    @property
    def is_success(self) -> bool:
        """判断响应是否成功（ok的别名）"""
        return self.ok

    @property
    def is_redirect(self) -> bool:
        """判断是否为重定向响应"""
        return 300 <= self.status_code < 400

    @property
    def is_client_error(self) -> bool:
        """判断是否为客户端错误"""
        return 400 <= self.status_code < 500

    @property
    def is_server_error(self) -> bool:
        """判断是否为服务端错误"""
        return 500 <= self.status_code < 600

    def json(self) -> Dict[str, Any]:
        """
        解析JSON响应

        Returns:
            Dict:  解析后的JSON数据

        Raises:
            ValueError: 当响应不是有效JSON时
        """
        if not self.text:
            raise ValueError("Response has no text content")

        try:
            return json_module.loads(self.text)
        except json_module.JSONDecodeError as e:
            raise ValueError(f"Response is not valid JSON: {e}")

    def raise_for_status(self):
        """
        如果响应状态码表示错误，抛出异常

        Raises:
            Exception: 当状态码表示错误或存在异常时
        """
        if self.exception:
            raise self.exception

        if not self.ok:
            error_msg = f"HTTP {self.status_code} Error for url: {self.url}"
            if self.text:
                error_msg += f"\nResponse: {self.text[:200]}"
            raise Exception(error_msg)

    def get_content_type(self) -> Optional[str]:
        """获取内容类型"""
        return self.headers.get("content-type") or self.headers.get("Content-Type")

    def get_encoding(self) -> str:
        """获取编码类型"""
        content_type = self.get_content_type()
        if content_type and "charset=" in content_type:
            return content_type.split("charset=")[1].split(";")[0].strip()
        return "utf-8"

    @classmethod
    def error_response(cls, url: str, exception: Exception, status_code: int = -1) -> "Response":
        """
        创建错误响应

        Args:
            url:  请求URL
            exception: 异常对象
            status_code: 状态码（默认-1表示网络错误）

        Returns:
            Response: 错误响应对象
        """
        return cls(
            url=url,
            status_code=status_code,
            content=None,
            text=str(exception),
            headers={},
            raw_response=None,
            exception=exception,
        )

    @classmethod
    def success_response(
        cls, url: str, content: bytes = b"", status_code: int = 200, headers: Optional[Dict[str, str]] = None
    ) -> "Response":
        """
        创建成功响应

        Args:
            url: 请求URL
            content: 响应内容
            status_code: 状态码
            headers: 响应头

        Returns:
            Response: 成功响应对象
        """
        return cls(
            url=url,
            status_code=status_code,
            content=content,
            text=content.decode("utf-8", errors="ignore") if content else "",
            headers=headers or {},
            raw_response=None,
            exception=None,
        )

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "url": self.url,
            "status_code": self.status_code,
            "headers": self.headers,
            "text": self.text,
            "elapsed_ms": self.elapsed_ms,
            "ok": self.ok,
            "exception": str(self.exception) if self.exception else None,
        }

    def __str__(self) -> str:
        """字符串表示"""
        return f"<Response [{self.status_code}] {self.url}>"

    def __repr__(self) -> str:
        """详细字符串表示"""
        return f"Response(url={self.url!r}, status_code={self.status_code}, elapsed_ms={self.elapsed_ms}, ok={self.ok})"
