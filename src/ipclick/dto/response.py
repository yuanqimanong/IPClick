"""适配器内部使用的轻量 HTTP 响应对象。"""

from collections.abc import Mapping
from dataclasses import dataclass
import json as json_module
from typing import Any

from typing_extensions import override

from ipclick.exceptions import RequestError


class Headers(dict[str, str]):
    """大小写不敏感的响应头映射，保留服务端给的原始拼写。

    HTTP 头字段本就大小写不敏感，但适配器之间不一致：curl_cffi 给全小写，
    niquests 保留原样。用普通 dict 装的话，照 curl_cffi 写好的
    ``headers.get("content-type")`` 换成 niquests 就返回 None——不报错、不告警，
    直接走进错误分支。这里让两种拼写都取得到，同时迭代和打印仍是原始拼写。

    拼写映射是**每次从字典现状算出来的**，不再另存一份小写索引。原来那份索引只在
    ``__setitem__`` / ``__delitem__`` 里维护，而 dict 的 ``update`` / ``pop`` /
    ``setdefault`` / ``clear`` / ``|=`` 都是继承来的、绕过它们：

    - ``h.update({"set-cookie": ...})`` 之后 ``h.get("Set-Cookie")`` 仍是 None——
      正是这个类要防的那个失败又回来了；
    - ``h.pop("Content-Type")`` 之后索引里还留着它，于是 ``"content-type" in h``
      为真，而 ``h.get("content-type", "X")`` **抛 KeyError**——带默认值的 get
      永远不该抛。

    改成派生之后，这些继承方法自动保持一致，不必再逐个补override。头字段只有几十个，
    线性扫描的代价可以忽略。
    """

    def __init__(self, data: Mapping[str, str] | None = None) -> None:
        """按原始拼写存储；同一字段的多种拼写只保留最后一个。"""
        super().__init__()
        self.update(data or {})

    def _actual(self, key: str) -> str:
        """把任意拼写映射到字典里实际在用的那个键。"""
        if super().__contains__(key):
            return key
        lowered = key.lower()
        return next((existing for existing in self if existing.lower() == lowered), key)

    @override
    def __getitem__(self, key: str) -> str:
        return super().__getitem__(self._actual(key))

    @override
    def __setitem__(self, key: str, value: str) -> None:
        actual = self._actual(key)
        if actual != key:
            super().__delitem__(actual)
        super().__setitem__(key, value)

    @override
    def __delitem__(self, key: str) -> None:
        super().__delitem__(self._actual(key))

    @override
    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and super().__contains__(self._actual(key))

    @override
    def get(self, key: str, default: str | None = None) -> str | None:
        actual = self._actual(key)
        return super().__getitem__(actual) if super().__contains__(actual) else default

    @override
    def pop(self, key: str, *args: Any) -> Any:
        return super().pop(self._actual(key), *args)

    @override
    def setdefault(self, key: str, default: str = "") -> str:
        actual = self._actual(key)
        if super().__contains__(actual):
            return super().__getitem__(actual)
        self[key] = default
        return default

    @override
    def update(self, *args: Any, **kwargs: str) -> None:
        """逐项走 __setitem__，让同一字段的不同拼写互相覆盖而不是并存。"""
        for source in args:
            items: Any = source.items() if isinstance(source, Mapping) else source
            for key, value in items:
                self[str(key)] = value
        for key, value in kwargs.items():
            self[key] = value

    @override
    def copy(self) -> "Headers":
        """返回同类型副本；继承 dict.copy 会退化成普通 dict、丢掉大小写不敏感。"""
        return Headers(self)

    @override
    def __ior__(self, other: Any) -> "Headers":
        self.update(other)
        return self


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
                # headers 不是映射时 encoding_from_headers 会抛 AttributeError。正文本身
                # 仍要交出去，按 UTF-8 兜底解码即可——原来是 str(self.content)，那给出的是
                # "b'...'" 这个 repr（带引号和转义的一整坨），当响应体是错的。
                self.text = self.content.decode("utf-8", errors="replace")

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
