from dataclasses import dataclass
import json as json_module
from typing import Any

from typing_extensions import override

from ipclick.exceptions import RequestError


@dataclass
class Response:
    url: str
    status_code: int
    content: bytes | None = None
    text: str | None = None
    headers: dict[str, str] | None = None
    raw_response: Any | None = None
    exception: Exception | None = None
    elapsed_ms: int = 0
    attempts: int = 1

    def __post_init__(self):
        if self.text is None and self.content is not None:
            try:
                self.text = self.content.decode("utf-8", errors="ignore")
            except (AttributeError, UnicodeDecodeError):
                self.text = str(self.content)

        if self.headers is None:
            self.headers = {}

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300 and self.exception is None

    @property
    def is_success(self) -> bool:
        return self.ok

    @property
    def is_redirect(self) -> bool:
        return 300 <= self.status_code < 400

    @property
    def is_client_error(self) -> bool:
        return 400 <= self.status_code < 500

    @property
    def is_server_error(self) -> bool:
        return 500 <= self.status_code < 600

    def json(self) -> Any:
        if not self.text:
            raise ValueError("Response has no text content")

        try:
            return json_module.loads(self.text)
        except json_module.JSONDecodeError as e:
            raise ValueError(f"Response is not valid JSON: {e}") from e

    def raise_for_status(self) -> None:
        if self.exception:
            raise self.exception

        if not self.ok:
            error_msg = f"HTTP {self.status_code} Error for url: {self.url}"
            if self.text:
                error_msg += f"\nResponse: {self.text[:200]}"
            raise RequestError(error_msg)

    def get_content_type(self) -> str | None:
        headers = self.headers or {}
        return headers.get("content-type") or headers.get("Content-Type")

    def get_encoding(self) -> str:
        content_type = self.get_content_type()
        if content_type and "charset=" in content_type:
            return content_type.split("charset=")[1].split(";")[0].strip()
        return "utf-8"

    @classmethod
    def error_response(cls, url: str, exception: Exception, status_code: int = -1, attempts: int = 1) -> "Response":
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
        return cls(
            url=url,
            status_code=status_code,
            content=content,
            text=content.decode("utf-8", errors="ignore") if content else "",
            headers=headers or {},
            raw_response=None,
            exception=None,
        )

    def to_dict(self) -> dict[str, Any]:
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
        return f"<Response [{self.status_code}] {self.url}>"

    @override
    def __repr__(self) -> str:
        return f"Response(url={self.url!r}, status_code={self.status_code}, elapsed_ms={self.elapsed_ms}, ok={self.ok})"
