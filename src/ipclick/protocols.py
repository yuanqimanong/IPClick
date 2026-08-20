"""客户端、流式正文与可分片限流器的结构化接口。"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any, Protocol, Self, runtime_checkable

from ipclick.dto.models import DownloadResponse, DownloadTask
from ipclick.limiter import LimiterSettings


@runtime_checkable
class StreamedBody(Protocol):
    """同步流式响应需要满足的最小接口。"""

    status_code: int
    headers: dict[str, str]
    error: str | None
    content_length: int
    total_bytes: int
    trailer_error: str | None

    def read(self) -> bytes:
        """读取剩余响应体。"""
        ...

    def close(self) -> None:
        """关闭或取消响应流。"""
        ...

    def __iter__(self) -> Iterator[bytes]: ...

    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None: ...


@runtime_checkable
class DownloadClient(Protocol):
    """独立和集群同步下载客户端共享的公开接口。"""

    def download(self, task: DownloadTask) -> DownloadResponse:
        """提交完整下载任务。"""
        ...

    def request(self, *, url: str, **kwargs: Any) -> DownloadResponse:
        """根据便捷参数构造并提交请求。"""
        ...

    def stream(self, url: str, **kwargs: Any) -> StreamedBody:
        """发起流式请求。"""
        ...

    def batch(self, tasks: Iterable[DownloadTask], timeout: float | None = None) -> Iterator[DownloadResponse]:
        """批量提交任务并产出响应。"""
        ...

    def get(self, url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> DownloadResponse:
        """发送 GET 请求。"""
        ...

    def post(self, url: str, data: Any = None, json: dict[str, Any] | None = None, **kwargs: Any) -> DownloadResponse:
        """发送 POST 请求。"""
        ...

    def put(self, url: str, data: Any = None, **kwargs: Any) -> DownloadResponse:
        """发送 PUT 请求。"""
        ...

    def patch(self, url: str, data: Any = None, **kwargs: Any) -> DownloadResponse:
        """发送 PATCH 请求。"""
        ...

    def delete(self, url: str, **kwargs: Any) -> DownloadResponse:
        """发送 DELETE 请求。"""
        ...

    def head(self, url: str, **kwargs: Any) -> DownloadResponse:
        """发送 HEAD 请求。"""
        ...

    def options(self, url: str, **kwargs: Any) -> DownloadResponse:
        """发送 OPTIONS 请求。"""
        ...

    def close(self) -> None:
        """释放客户端资源。"""
        ...

    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None: ...


@runtime_checkable
class ShardableLimiter(Protocol):
    """能够按存活节点数调整份额的限流器接口。"""

    @property
    def settings(self) -> LimiterSettings:
        """返回限流器配置。"""
        ...

    def set_cluster_size(self, live_nodes: int) -> None:
        """按存活节点数调整本实例份额。"""
        ...


__all__ = ["DownloadClient", "ShardableLimiter", "StreamedBody"]
