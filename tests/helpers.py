from __future__ import annotations

import time
from typing import Any

from typing_extensions import override

from ipclick.adapters.base import DownloaderAdapter
from ipclick.adapters.settings import AdapterSettings
from ipclick.dto.response import Response


class FakeClock:
    def __init__(self) -> None:
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    def monotonic(self) -> float:
        return time.monotonic()

    def time(self) -> float:
        return time.time()


class FakeAsyncClock:
    def __init__(self) -> None:
        self.slept: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


class RecordingContext:
    def __init__(self, *, active: bool = True, metadata: tuple[tuple[str, str], ...] = ()) -> None:
        self.code: Any = None
        self.details: str = ""
        self._active: bool = active
        self._metadata: tuple[tuple[str, str], ...] = metadata

    def set_code(self, code: Any) -> None:
        self.code = code

    def set_details(self, details: str) -> None:
        self.details = details

    def is_active(self) -> bool:
        return self._active

    def invocation_metadata(self) -> tuple[tuple[str, str], ...]:
        return self._metadata


class StubAdapter(DownloaderAdapter):
    adapter_name: str = "curl_cffi"

    def __init__(self, settings: AdapterSettings | None = None) -> None:
        super().__init__(settings)
        self.seen: list[tuple[str, dict[str, Any]]] = []
        self.response: Response | None = None
        self.raises: Exception | None = None

    @override
    def download(self, url: str, **kwargs: Any) -> Response:
        self.seen.append((url, kwargs))
        if self.raises is not None:
            raise self.raises
        return self.response or Response(url=url, status_code=200, content=b"body", headers={"x-stub": "1"})
