"""断点续传。

用一个可以按指令"断在第 N 字节"的假流式客户端来驱动——真起一个会断的
HTTP 服务端很难精确控制断点，而这里要验的是续传逻辑本身。
"""

from pathlib import Path
from typing import Any

import pytest

from ipclick.exceptions import TransportError, ValidationError
from ipclick.resume import download_to_file, iter_resumable


BODY = bytes(range(256)) * 40  # 10240 字节


class _FakeStream:
    """模拟 StreamedResponse。"""

    def __init__(
        self,
        payload: bytes,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        break_after: int | None = None,
        content_length: int = -1,
    ):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.content_length = content_length
        self.trailer_error: str | None = None
        self._break_after = break_after

    def __enter__(self) -> "_FakeStream":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def __iter__(self):
        sent = 0
        step = 1024
        for start in range(0, len(self._payload), step):
            chunk = self._payload[start : start + step]
            if self._break_after is not None and sent + len(chunk) > self._break_after:
                # 先把断点之前的部分发出去，再宣告中断
                remainder = self._break_after - sent
                if remainder > 0:
                    yield chunk[:remainder]
                raise TransportError("连接被重置")
            yield chunk
            sent += len(chunk)


class _FakeClient:
    """按脚本回放若干次 stream() 调用。"""

    def __init__(self, script: list[_FakeStream]):
        self._script = script
        self.calls: list[dict[str, Any]] = []

    def stream(self, url: str, **kwargs: Any) -> _FakeStream:
        self.calls.append({"url": url, **kwargs})
        if not self._script:
            raise AssertionError("stream() 调用次数超出脚本预期")
        return self._script.pop(0)

    @property
    def range_headers(self) -> list[str | None]:
        return [dict(c.get("headers") or {}).get("Range") for c in self.calls]

    @property
    def if_range_headers(self) -> list[str | None]:
        return [dict(c.get("headers") or {}).get("If-Range") for c in self.calls]


_RANGE_OK = {"Accept-Ranges": "bytes", "ETag": '"v1"'}


class TestHappyPath:
    def test_single_shot(self, tmp_path: Path):
        client = _FakeClient([_FakeStream(BODY, headers=_RANGE_OK, content_length=len(BODY))])
        result = download_to_file(client, "http://x/f", tmp_path / "f.bin")
        assert result.total_bytes == len(BODY)
        assert result.attempts == 1
        assert not result.resumed
        assert (tmp_path / "f.bin").read_bytes() == BODY

    def test_creates_parent_dirs(self, tmp_path: Path):
        client = _FakeClient([_FakeStream(BODY, headers=_RANGE_OK)])
        target = tmp_path / "a" / "b" / "f.bin"
        download_to_file(client, "http://x/f", target)
        assert target.exists()

    def test_progress_callback(self, tmp_path: Path):
        client = _FakeClient([_FakeStream(BODY, headers=_RANGE_OK, content_length=len(BODY))])
        seen: list[tuple[int, int]] = []
        download_to_file(client, "http://x/f", tmp_path / "f.bin", chunk_callback=lambda d, t: seen.append((d, t)))
        assert seen[-1] == (len(BODY), len(BODY))
        assert seen[0][0] < seen[-1][0], "进度应当是递增的"

    def test_http_error_returns_without_retry(self, tmp_path: Path):
        """404 换多少次也是 404，不该浪费重试次数。"""
        client = _FakeClient([_FakeStream(b"", status_code=404)])
        result = download_to_file(client, "http://x/f", tmp_path / "f.bin")
        assert result.status_code == 404
        assert result.attempts == 1


class TestResume:
    def test_resumes_from_break_point(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("ipclick.resume._RETRY_BACKOFF", 0)
        cut = 4096
        client = _FakeClient(
            [
                _FakeStream(BODY, headers=_RANGE_OK, content_length=len(BODY), break_after=cut),
                _FakeStream(BODY[cut:], status_code=206, headers=_RANGE_OK),
            ]
        )
        result = download_to_file(client, "http://x/f", tmp_path / "f.bin")
        assert result.attempts == 2
        assert result.resumed
        assert (tmp_path / "f.bin").read_bytes() == BODY, "续传拼出来的内容必须和原文件一致"

    def test_sends_range_and_if_range(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """If-Range 是防止拼接两个版本的关键，必须带上。"""
        monkeypatch.setattr("ipclick.resume._RETRY_BACKOFF", 0)
        cut = 2048
        client = _FakeClient(
            [
                _FakeStream(BODY, headers=_RANGE_OK, break_after=cut),
                _FakeStream(BODY[cut:], status_code=206, headers=_RANGE_OK),
            ]
        )
        download_to_file(client, "http://x/f", tmp_path / "f.bin")
        assert client.range_headers == [None, f"bytes={cut}-"]
        assert client.if_range_headers == [None, '"v1"']

    def test_falls_back_to_last_modified(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("ipclick.resume._RETRY_BACKOFF", 0)
        headers = {"Accept-Ranges": "bytes", "Last-Modified": "Wed, 21 Oct 2026 07:28:00 GMT"}
        client = _FakeClient(
            [
                _FakeStream(BODY, headers=headers, break_after=1024),
                _FakeStream(BODY[1024:], status_code=206, headers=headers),
            ]
        )
        download_to_file(client, "http://x/f", tmp_path / "f.bin")
        assert client.if_range_headers[1] == "Wed, 21 Oct 2026 07:28:00 GMT"

    def test_multiple_breaks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("ipclick.resume._RETRY_BACKOFF", 0)
        client = _FakeClient(
            [
                _FakeStream(BODY, headers=_RANGE_OK, break_after=1024),
                _FakeStream(BODY[1024:], status_code=206, headers=_RANGE_OK, break_after=2048),
                _FakeStream(BODY[3072:], status_code=206, headers=_RANGE_OK),
            ]
        )
        result = download_to_file(client, "http://x/f", tmp_path / "f.bin")
        assert result.attempts == 3
        assert (tmp_path / "f.bin").read_bytes() == BODY

    def test_truncated_body_is_treated_as_interruption(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """声明了 content-length 却只给一半，是被截断的流，不能当成功。"""
        monkeypatch.setattr("ipclick.resume._RETRY_BACKOFF", 0)
        half = len(BODY) // 2
        client = _FakeClient(
            [
                _FakeStream(BODY[:half], headers=_RANGE_OK, content_length=len(BODY)),
                _FakeStream(BODY[half:], status_code=206, headers=_RANGE_OK),
            ]
        )
        result = download_to_file(client, "http://x/f", tmp_path / "f.bin")
        assert result.attempts == 2
        assert (tmp_path / "f.bin").read_bytes() == BODY


class TestVersionSafety:
    """最要紧的一组：绝不能把两个版本的内容拼在一起。"""

    def test_changed_resource_restarts_from_scratch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """服务端对 If-Range 判定资源已变，回 200 + 完整新内容。

        这时必须丢掉已下载的旧内容重来。接着写就会拼出一个既不是旧版、
        也不是新版、而且校验不出来的文件——比下载失败严重得多。
        """
        monkeypatch.setattr("ipclick.resume._RETRY_BACKOFF", 0)
        new_body = b"NEW" * 2000
        client = _FakeClient(
            [
                _FakeStream(BODY, headers=_RANGE_OK, break_after=2048),
                # 200 而不是 206：资源变了，这是完整的新内容
                _FakeStream(new_body, status_code=200, headers=_RANGE_OK),
            ]
        )
        result = download_to_file(client, "http://x/f", tmp_path / "f.bin")
        assert result.restarts == 1
        assert (tmp_path / "f.bin").read_bytes() == new_body, "旧内容没被丢掉，文件被拼坏了"

    def test_server_without_range_support_restarts(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """没有 Accept-Ranges 就别发 Range，中断后整体重下。"""
        monkeypatch.setattr("ipclick.resume._RETRY_BACKOFF", 0)
        no_range = {"ETag": '"v1"'}
        client = _FakeClient(
            [
                _FakeStream(BODY, headers=no_range, break_after=2048),
                _FakeStream(BODY, headers=no_range),
            ]
        )
        result = download_to_file(client, "http://x/f", tmp_path / "f.bin")
        assert client.range_headers == [None, None], "服务端不支持 Range 时不该发 Range 头"
        assert result.restarts == 1
        assert (tmp_path / "f.bin").read_bytes() == BODY


class TestExhaustion:
    def test_raises_after_max_attempts(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("ipclick.resume._RETRY_BACKOFF", 0)
        client = _FakeClient([_FakeStream(BODY, headers=_RANGE_OK, break_after=512) for _ in range(3)])
        with pytest.raises(TransportError, match="仍未下载完整"):
            download_to_file(client, "http://x/f", tmp_path / "f.bin", max_attempts=3)

    def test_rejects_bad_max_attempts(self, tmp_path: Path):
        with pytest.raises(ValidationError):
            download_to_file(_FakeClient([]), "http://x/f", tmp_path / "f.bin", max_attempts=0)


class TestIterResumable:
    def test_yields_all_chunks(self):
        client = _FakeClient([_FakeStream(BODY, headers=_RANGE_OK)])
        assert b"".join(iter_resumable(client, "http://x/f")) == BODY

    def test_resumes_mid_stream(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("ipclick.resume._RETRY_BACKOFF", 0)
        cut = 3072
        client = _FakeClient(
            [
                _FakeStream(BODY, headers=_RANGE_OK, break_after=cut),
                _FakeStream(BODY[cut:], status_code=206, headers=_RANGE_OK),
            ]
        )
        assert b"".join(iter_resumable(client, "http://x/f")) == BODY

    def test_changed_resource_raises_instead_of_mixing(self, monkeypatch: pytest.MonkeyPatch):
        """迭代器已经把分片交出去了，收不回来。

        这时如果服务端回 200（资源变了），继续产出就等于让调用方拿到两个版本
        的混合体。只能抛错，让调用方决定是否整体重来。
        """
        monkeypatch.setattr("ipclick.resume._RETRY_BACKOFF", 0)
        client = _FakeClient(
            [
                _FakeStream(BODY, headers=_RANGE_OK, break_after=1024),
                _FakeStream(b"NEW" * 100, status_code=200, headers=_RANGE_OK),
            ]
        )
        with pytest.raises(TransportError, match="无法从"):
            list(iter_resumable(client, "http://x/f"))
