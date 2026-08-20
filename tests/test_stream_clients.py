from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Generator, Iterator
import threading
from typing import Any, cast

import pytest
from typing_extensions import override

from ipclick.aio import _EOF, AsyncStreamedResponse
from ipclick.dto.proto import task_pb2
from ipclick.exceptions import ClientClosedError, TransportError
import ipclick.sdk as sdk_module
from ipclick.sdk import Downloader, StreamedResponse


def _header() -> Any:
    return task_pb2.TaskRespChunk(
        header=task_pb2.TaskRespHeader(
            request_uuid="request-1",
            effective_url="https://example.com/file",
            status_code=200,
            content_length=3,
        )
    )


def _chunk(data: bytes = b"abc") -> Any:
    return task_pb2.TaskRespChunk(chunk=data)


def _trailer() -> Any:
    return task_pb2.TaskRespChunk(trailer=task_pb2.TaskRespTrailer(total_bytes=3, response_time_ms=7))


class FakeSyncCall:
    def __init__(self, messages: list[Any]) -> None:
        self._messages: list[Any] = messages
        self.cancelled: bool = False

    def __iter__(self) -> Iterator[Any]:
        return iter(self._messages)

    def cancel(self) -> None:
        self.cancelled = True


class FakeAsyncCall:
    def __init__(self, messages: list[Any]) -> None:
        self._messages: list[Any] = messages
        self.cancelled: bool = False

    async def read(self) -> Any:
        return self._messages.pop(0) if self._messages else _EOF

    def cancel(self) -> None:
        self.cancelled = True


def test_sync_stream_requires_trailer_and_cancels_rpc() -> None:
    call = FakeSyncCall([_header(), _chunk()])
    response = StreamedResponse(call)

    with pytest.raises(TransportError, match="trailer"):
        response.read()

    assert call.cancelled is True


def test_sync_empty_stream_cancels_rpc_during_construction() -> None:
    call = FakeSyncCall([])

    with pytest.raises(TransportError, match="未返回任何数据"):
        StreamedResponse(call)

    assert call.cancelled is True


def test_sync_stream_early_close_cancels_rpc() -> None:
    call = FakeSyncCall([_header(), _chunk(), _trailer()])
    response = StreamedResponse(call)
    iterator = cast(Generator[bytes, None, None], iter(response))

    assert next(iterator) == b"abc"
    iterator.close()

    assert call.cancelled is True
    assert list(response) == []


def test_sync_stream_with_trailer_completes_without_cancel() -> None:
    call = FakeSyncCall([_header(), _chunk(), _trailer()])
    response = StreamedResponse(call)

    assert response.read() == b"abc"
    response.close()

    assert response.total_bytes == 3
    assert call.cancelled is False


def test_sync_stream_trailer_error_marks_response_unsuccessful() -> None:
    trailer = task_pb2.TaskRespChunk(trailer=task_pb2.TaskRespTrailer(error_message="upstream reset"))
    response = StreamedResponse(FakeSyncCall([_header(), trailer]))

    assert response.read() == b""
    assert response.is_success() is False


async def test_async_stream_requires_trailer_and_cancels_rpc() -> None:
    call = FakeAsyncCall([_header(), _chunk()])
    response = await AsyncStreamedResponse.create(call)

    with pytest.raises(TransportError, match="trailer"):
        await response.read()

    assert call.cancelled is True


async def test_async_empty_stream_cancels_rpc_during_construction() -> None:
    call = FakeAsyncCall([])

    with pytest.raises(TransportError, match="未返回任何数据"):
        await AsyncStreamedResponse.create(call)

    assert call.cancelled is True


async def test_async_stream_header_cancellation_cancels_rpc() -> None:
    class CancelledCall(FakeAsyncCall):
        @override
        async def read(self) -> Any:
            raise asyncio.CancelledError

    call = CancelledCall([])
    with pytest.raises(asyncio.CancelledError):
        await AsyncStreamedResponse.create(call)

    assert call.cancelled is True


async def test_async_stream_early_aclose_cancels_rpc() -> None:
    call = FakeAsyncCall([_header(), _chunk(), _trailer()])
    response = await AsyncStreamedResponse.create(call)
    iterator = cast(AsyncGenerator[bytes, None], response.__aiter__())

    assert await anext(iterator) == b"abc"
    await iterator.aclose()

    assert call.cancelled is True
    assert [chunk async for chunk in response] == []


async def test_async_stream_with_trailer_completes_without_cancel() -> None:
    call = FakeAsyncCall([_header(), _chunk(), _trailer()])
    response = await AsyncStreamedResponse.create(call)

    assert await response.read() == b"abc"
    await response.aclose()

    assert response.total_bytes == 3
    assert call.cancelled is False


class _OrderedLock:
    """让 close 确定先于被阻塞的首次建连进入临界区。"""

    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        self.getter_waiting: threading.Event = threading.Event()
        self.allow_getter: threading.Event = threading.Event()

    def __enter__(self) -> None:
        if threading.current_thread().name == "getter":
            self.getter_waiting.set()
            assert self.allow_getter.wait(2)
        self._lock.acquire()

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self._lock.release()


def test_sync_downloader_close_wins_first_stub_creation_race(monkeypatch: pytest.MonkeyPatch) -> None:
    downloader: Any = object.__new__(Downloader)
    ordered = _OrderedLock()
    downloader._closed = False
    downloader._stub = None
    downloader._channel = None
    downloader._lock = ordered
    downloader.host = "127.0.0.1"
    downloader.port = 8799
    downloader._credentials = None
    downloader._channel_options = []

    opened = False

    def fail_if_opened(*_args: Any, **_kwargs: Any) -> None:
        nonlocal opened
        opened = True

    monkeypatch.setattr(sdk_module, "open_channel", fail_if_opened)
    errors: list[Exception] = []

    def get_stub() -> None:
        try:
            downloader._get_stub()
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=get_stub, name="getter")
    worker.start()
    assert ordered.getter_waiting.wait(2)

    downloader.close()
    ordered.allow_getter.set()
    worker.join(2)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], ClientClosedError)
    assert opened is False


def test_sync_downloader_close_wins_existing_stub_race() -> None:
    class FakeChannel:
        def close(self) -> None:
            return None

    downloader: Any = object.__new__(Downloader)
    ordered = _OrderedLock()
    downloader._closed = False
    downloader._stub = object()
    downloader._channel = FakeChannel()
    downloader._lock = ordered
    errors: list[Exception] = []

    def get_stub() -> None:
        try:
            downloader._get_stub()
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=get_stub, name="getter")
    worker.start()
    assert ordered.getter_waiting.wait(2)
    downloader.close()
    ordered.allow_getter.set()
    worker.join(2)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], ClientClosedError)


@pytest.mark.parametrize(("host", "target"), [("::1", "[::1]:8799"), ("[::1]", "[::1]:8799")])
def test_client_target_formats_ipv6(host: str, target: str) -> None:
    downloader: Any = object.__new__(Downloader)
    downloader.host = host
    downloader.port = 8799
    assert downloader.target == target
