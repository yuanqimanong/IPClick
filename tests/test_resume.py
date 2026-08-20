from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ipclick.exceptions import TransportError
from ipclick.resume import download_to_file, iter_resumable


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int,
        headers: dict[str, str],
        content_length: int,
        chunks: list[bytes],
        error_after_chunks: str | None = None,
    ) -> None:
        self.status_code: int = status_code
        self.headers: dict[str, str] = headers
        self.error: str | None = None
        self.content_length: int = content_length
        self.total_bytes: int = sum(len(chunk) for chunk in chunks)
        self.trailer_error: str | None = None
        self._chunks: list[bytes] = chunks
        self._error_after_chunks: str | None = error_after_chunks

    def __iter__(self) -> Iterator[bytes]:
        yield from self._chunks
        if self._error_after_chunks is not None:
            raise TransportError(self._error_after_chunks)

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        return None

    def read(self) -> bytes:
        return b"".join(self)

    def close(self) -> None:
        return None


class FakeStreamer:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses: list[FakeResponse] = responses
        self.urls: list[str] = []
        self.calls: list[dict[str, Any]] = []

    def stream(self, url: str, **kwargs: Any) -> FakeResponse:
        self.urls.append(url)
        self.calls.append(kwargs)
        return self._responses.pop(0)


def _initial_partial() -> FakeResponse:
    return FakeResponse(
        status_code=200,
        headers={"Accept-Ranges": "bytes", "ETag": '"v1"', "Content-Length": "6"},
        content_length=6,
        chunks=[b"abc"],
        error_after_chunks="connection reset",
    )


def _partial_response(content_range: str, *, etag: str | None = None) -> FakeResponse:
    headers = {"Content-Range": content_range, "Content-Length": "3"}
    if etag is not None:
        headers["ETag"] = etag
    return FakeResponse(
        status_code=206,
        headers=headers,
        content_length=3,
        chunks=[b"def"],
    )


def test_download_to_file_validates_and_appends_matching_range(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ipclick.resume.time.sleep", lambda _: None)
    client = FakeStreamer([_initial_partial(), _partial_response("bytes 3-5/6")])
    target = tmp_path / "file.bin"

    result = download_to_file(client, "https://example.com/file", target)

    assert result.total_bytes == 6
    assert result.attempts == 2
    assert target.read_bytes() == b"abcdef"
    assert client.calls[1]["headers"] == {"Range": "bytes=3-", "If-Range": '"v1"'}


@pytest.mark.parametrize(
    "content_range",
    [
        "bytes 2-4/6",
        "bytes 3-5/7",
        "bytes 3-5/*",
        "",
    ],
)
def test_download_to_file_rejects_invalid_range_before_append(
    content_range: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ipclick.resume.time.sleep", lambda _: None)
    client = FakeStreamer([_initial_partial(), _partial_response(content_range)])
    target = tmp_path / "file.bin"

    with pytest.raises(TransportError, match="续传"):
        download_to_file(client, "https://example.com/file", target)

    assert target.read_bytes() == b"abc"
    assert len(client.calls) == 2


def test_iter_resumable_rejects_changed_total_before_yielding_bad_range(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ipclick.resume.time.sleep", lambda _: None)
    client = FakeStreamer([_initial_partial(), _partial_response("bytes 3-5/7")])
    chunks: list[bytes] = []

    with pytest.raises(TransportError, match="总长度"):
        for chunk in iter_resumable(client, "https://example.com/file"):
            chunks.append(chunk)

    assert chunks == [b"abc"]


def test_download_to_file_rejects_changed_validator_before_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ipclick.resume.time.sleep", lambda _: None)
    client = FakeStreamer([_initial_partial(), _partial_response("bytes 3-5/6", etag='"v2"')])
    target = tmp_path / "file.bin"

    with pytest.raises(TransportError, match="校验器"):
        download_to_file(client, "https://example.com/file", target)

    assert target.read_bytes() == b"abc"
