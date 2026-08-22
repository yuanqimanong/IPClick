from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import threading
from typing import Any, final

import pytest
from typing_extensions import override

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
    written: list[int] = []

    with pytest.raises(TransportError, match="续传"):
        _ = download_to_file(
            client, "https://example.com/file", target, chunk_callback=lambda done, _t: written.append(done)
        )

    # 核心不变量：不一致的区间一个字节都没落盘——写入量停在第一次那 3 字节
    assert written == [3]
    # 半成品也不再留在最终路径上（改成先写临时文件、完整才原子改名），临时文件已清掉
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []
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

    written: list[int] = []

    with pytest.raises(TransportError, match="校验器"):
        _ = download_to_file(
            client, "https://example.com/file", target, chunk_callback=lambda done, _t: written.append(done)
        )

    # 校验器变了就不能拼接：第二段一个字节都没落盘，最终路径上也没有半成品
    assert written == [3]
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def _failing_stream(chunks: list[bytes], declared: int) -> FakeResponse:
    return FakeResponse(
        status_code=200,
        headers={"Content-Length": str(declared)},
        content_length=declared,
        chunks=chunks,
        error_after_chunks="connection reset",
    )


def test_a_failed_download_leaves_the_target_untouched(tmp_path: Path) -> None:
    """下载失败不能在最终路径上留下半成品，也不能把原有的完整文件截断。

    原来直接写最终路径，于是：失败后 target 存在且是截断的（按"文件存在即已下载"
    判断的消费者读到的是坏数据），而且第一个字节到达之前就把旧文件截断掉了。
    异常文本里的字节数也和磁盘对不上——downloaded 在不可续传时被重置成 0。
    """
    target = tmp_path / "file.bin"
    _ = target.write_bytes(b"OLD-COMPLETE-CONTENT")
    client = FakeStreamer([_failing_stream([b"x" * 300], 1000), _failing_stream([b"x" * 300], 1000)])

    with pytest.raises(TransportError) as excinfo:
        _ = download_to_file(client, "https://example.com/f", target, max_attempts=2)

    assert target.read_bytes() == b"OLD-COMPLETE-CONTENT", "失败却改动了目标文件"
    assert list(tmp_path.iterdir()) == [target], f"留下了临时文件：{list(tmp_path.iterdir())}"
    assert "目标文件未改动" in str(excinfo.value)


def test_a_non_2xx_response_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    """4xx/5xx 直接返回，同样不能留下临时文件。"""
    target = tmp_path / "file.bin"
    client = FakeStreamer([FakeResponse(status_code=404, headers={}, content_length=0, chunks=[])])

    result = download_to_file(client, "https://example.com/f", target, max_attempts=2)

    assert result.status_code == 404
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_the_target_is_not_touched_until_the_download_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """下载期间最终路径不得出现——这是"两个并发下载不会互相截断"的充分条件。

    原来整个过程都直接写最终路径，所以两个并发下载必然互相干扰：A 断线后以 "ab"
    续传，B 同时以 "wb" 从头重下，双方都返回 total_bytes 正确的成功结果，磁盘上的
    文件却既不是 A 也不是 B。
    """
    monkeypatch.setattr("ipclick.resume.time.sleep", lambda _: None)
    client = FakeStreamer([_initial_partial(), _partial_response("bytes 3-5/6")])
    target = tmp_path / "file.bin"
    existed_during: list[bool] = []

    result = download_to_file(
        client,
        "https://example.com/file",
        target,
        chunk_callback=lambda _done, _total: existed_during.append(target.exists()),
    )

    assert existed_during, "chunk_callback 一次都没被调用，这条用例没测到东西"
    assert not any(existed_during), "下载还没完成，最终路径就已经被写了"
    assert result.total_bytes == 6
    assert target.read_bytes() == b"abcdef"


def test_concurrent_resume_and_restart_on_one_target_cannot_interleave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """确定性地把两个下载排成最坏顺序：A 续传的追加写必须发生在 B 整体写完之后。

    时序由事件锁死，不赌调度：A 写完前半 -> B 整体写完 -> A 追加后半。
    共用最终路径时这必然产出 A、B 交错的 9 字节文件，且两边都报成功。
    """
    monkeypatch.setattr("ipclick.resume.time.sleep", lambda _: None)
    target = tmp_path / "shared.bin"
    a_has_partial = threading.Event()
    b_finished = threading.Event()
    results: dict[str, Any] = {}
    errors: list[BaseException] = []

    @final
    class _GatedResume(FakeResponse):
        """A 的续传响应：等 B 彻底写完之后才吐字节。"""

        @override
        def __iter__(self) -> Iterator[bytes]:
            _ = b_finished.wait(timeout=10)
            yield from super().__iter__()

    def run_a() -> None:
        gated = _GatedResume(
            status_code=206,
            headers={"Content-Range": "bytes 3-5/6", "Content-Length": "3"},
            content_length=3,
            chunks=[b"def"],
        )
        client = FakeStreamer([_initial_partial(), gated])
        try:
            results["a"] = download_to_file(
                client,
                "https://example.com/file",
                target,
                chunk_callback=lambda _d, _t: a_has_partial.set(),
            )
        except BaseException as e:
            errors.append(e)

    def run_b() -> None:
        _ = a_has_partial.wait(timeout=10)
        client = FakeStreamer(
            [
                FakeResponse(
                    status_code=200,
                    headers={"Content-Length": "6"},
                    content_length=6,
                    chunks=[b"BBBBBB"],
                )
            ]
        )
        try:
            results["b"] = download_to_file(client, "https://example.com/file", target)
        except BaseException as e:
            errors.append(e)
        finally:
            b_finished.set()

    threads = [threading.Thread(target=run_a), threading.Thread(target=run_b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert set(results) == {"a", "b"}
    content = target.read_bytes()
    assert content in (b"abcdef", b"BBBBBB"), f"文件被交错写坏：{content!r}"
    assert list(tmp_path.iterdir()) == [target], f"留下了临时文件：{list(tmp_path.iterdir())}"


class _RaisingStreamer:
    """前几次建流直接抛 TransportError，之后交出一条正常的流。"""

    def __init__(self, failures: int, then: FakeResponse) -> None:
        self.failures: int = failures
        self.then: FakeResponse = then
        self.attempts: int = 0

    def stream(self, url: str, **kwargs: Any) -> FakeResponse:
        _ = (url, kwargs)
        self.attempts += 1
        if self.attempts <= self.failures:
            raise TransportError("connection refused")
        return self.then


def test_a_failure_before_the_first_response_still_honours_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """一个字节都还没产出时，重试次数必须按 max_attempts 走。

    range_ok 要等第一个响应到手才置位，而原来的退出判据是 `not range_ok or ...`：
    首个 client.stream() 就抛 TransportError（连接被拒、DNS 失败）时，调用方传的
    max_attempts=5 静默变成只试 1 次。已产出字节才需要服务端支持 Range——那些字节
    收不回来，得靠 206 接上；还没产出时重头再来是安全的。
    """
    monkeypatch.setattr("ipclick.resume.time.sleep", lambda _: None)
    body = FakeResponse(status_code=200, headers={"Content-Length": "3"}, content_length=3, chunks=[b"abc"])
    client = _RaisingStreamer(failures=2, then=body)

    chunks = list(iter_resumable(client, "https://example.com/f", max_attempts=5))

    assert b"".join(chunks) == b"abc"
    assert client.attempts == 3


def test_max_attempts_is_still_an_upper_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """放宽退出条件不能变成无限重试。"""
    monkeypatch.setattr("ipclick.resume.time.sleep", lambda _: None)
    body = FakeResponse(status_code=200, headers={"Content-Length": "3"}, content_length=3, chunks=[b"abc"])
    client = _RaisingStreamer(failures=99, then=body)

    with pytest.raises(TransportError, match="在 3 次尝试后仍未读完"):
        _ = list(iter_resumable(client, "https://example.com/f", max_attempts=3))

    assert client.attempts == 3


def test_a_server_without_range_support_is_not_resumed_after_yielding(monkeypatch: pytest.MonkeyPatch) -> None:
    """已经 yield 过字节、而服务端不支持 Range 时，仍然必须立刻放弃。"""
    monkeypatch.setattr("ipclick.resume.time.sleep", lambda _: None)
    no_range = FakeResponse(
        status_code=200,
        headers={"Content-Length": "6"},
        content_length=6,
        chunks=[b"abc"],
        error_after_chunks="connection reset",
    )
    client = FakeStreamer([no_range])

    with pytest.raises(TransportError):
        _ = list(iter_resumable(client, "https://example.com/f", max_attempts=5))

    # 只建了一次流：不能拿一个不支持 Range 的服务端反复重试
    assert len(client.urls) == 1
