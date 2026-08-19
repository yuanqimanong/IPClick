from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any, Protocol, Self, runtime_checkable

from ipclick.dto.models import DownloadResponse, DownloadTask
from ipclick.limiter import LimiterSettings


@runtime_checkable
class StreamedBody(Protocol):
    status_code: int
    headers: dict[str, str]
    error: str | None
    content_length: int
    total_bytes: int
    trailer_error: str | None

    def read(self) -> bytes: ...

    def close(self) -> None: ...

    def __iter__(self) -> Iterator[bytes]: ...

    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None: ...


@runtime_checkable
class DownloadClient(Protocol):
    def download(self, task: DownloadTask) -> DownloadResponse: ...

    def request(self, *, url: str, **kwargs: Any) -> DownloadResponse: ...

    def stream(self, url: str, **kwargs: Any) -> StreamedBody: ...

    def batch(self, tasks: Iterable[DownloadTask], timeout: float | None = None) -> Iterator[DownloadResponse]: ...

    def get(self, url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> DownloadResponse: ...

    def post(
        self, url: str, data: Any = None, json: dict[str, Any] | None = None, **kwargs: Any
    ) -> DownloadResponse: ...

    def put(self, url: str, data: Any = None, **kwargs: Any) -> DownloadResponse: ...

    def patch(self, url: str, data: Any = None, **kwargs: Any) -> DownloadResponse: ...

    def delete(self, url: str, **kwargs: Any) -> DownloadResponse: ...

    def head(self, url: str, **kwargs: Any) -> DownloadResponse: ...

    def options(self, url: str, **kwargs: Any) -> DownloadResponse: ...

    def close(self) -> None: ...

    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None: ...


@runtime_checkable
class ShardableLimiter(Protocol):
    @property
    def settings(self) -> LimiterSettings: ...

    def set_cluster_size(self, live_nodes: int) -> None: ...


__all__ = ["DownloadClient", "ShardableLimiter", "StreamedBody"]
