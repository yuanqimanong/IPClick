"""基于 HTTP Range 和校验器的可恢复流式下载工具。"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
import re
import time
from typing import Any, Protocol

from ipclick.exceptions import TransportError, ValidationError
from ipclick.protocols import StreamedBody
from ipclick.utils.log_util import log


_RETRY_BACKOFF = 1.0

_CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+)", re.IGNORECASE)


class _InvalidRangeError(TransportError):
    """服务端返回了不可安全拼接的续传区间。"""


class _Streamer(Protocol):
    def stream(self, url: str, **kwargs: Any) -> StreamedBody:
        """发起同步流式请求。"""
        ...


@dataclass
class ResumeResult:
    """文件下载结果及实际尝试、重启次数。"""

    path: Path
    total_bytes: int
    status_code: int
    attempts: int = 1
    restarts: int = 0
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def resumed(self) -> bool:
        """返回下载是否经历了不止一次传输尝试。"""
        return self.attempts > 1


def _validator(headers: dict[str, str]) -> str | None:
    lowered = {k.lower(): v for k, v in headers.items()}
    return lowered.get("etag") or lowered.get("last-modified")


def _supports_range(headers: dict[str, str]) -> bool:
    lowered = {k.lower(): v for k, v in headers.items()}
    return "bytes" in lowered.get("accept-ranges", "").lower()


def _validate_content_range(headers: dict[str, str], offset: int, expected: int, validator: str | None = None) -> int:
    """校验 206 响应与请求偏移一致，并返回资源总长度。"""
    lowered = {k.lower(): v for k, v in headers.items()}
    raw = lowered.get("content-range", "").strip()
    match = _CONTENT_RANGE_RE.fullmatch(raw)
    if match is None:
        raise _InvalidRangeError(f"续传响应缺少合法的 Content-Range：{raw or '<缺失>'}")

    start, end, total = (int(value) for value in match.groups())
    if start != offset:
        raise _InvalidRangeError(f"续传响应起点错误：请求从 {offset} 开始，Content-Range 却从 {start} 开始")
    if end < start or total <= end:
        raise _InvalidRangeError(f"续传响应区间非法：Content-Range={raw}")
    if expected >= 0 and total != expected:
        raise _InvalidRangeError(f"续传资源总长度发生变化：首次为 {expected}，本次为 {total}")
    current_validator = _validator(headers)
    if validator is not None and current_validator is not None and current_validator != validator:
        raise _InvalidRangeError(f"续传资源校验器发生变化：首次为 {validator!r}，本次为 {current_validator!r}")

    raw_length = lowered.get("content-length")
    if raw_length is not None:
        try:
            content_length = int(raw_length)
        except ValueError as e:
            raise _InvalidRangeError(f"续传响应的 Content-Length 非法：{raw_length!r}") from e
        range_length = end - start + 1
        if content_length != range_length:
            raise _InvalidRangeError(f"续传响应长度不一致：Content-Length={content_length}，区间长度={range_length}")
    return total


def download_to_file(
    client: _Streamer,
    url: str,
    path: str | Path,
    *,
    max_attempts: int = 5,
    chunk_callback: Callable[[int, int], None] | None = None,
    **kwargs: Any,
) -> ResumeResult:
    """将 URL 下载到文件，并在服务端支持 Range 时从断点续传。"""
    if max_attempts < 1:
        raise ValidationError(f"max_attempts 必须 >= 1，当前为 {max_attempts}")

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    downloaded = 0
    expected = -1
    validator: str | None = None
    range_ok = False
    attempts = 0
    restarts = 0
    status_code = -1
    headers: dict[str, str] = {}
    last_error: str = ""

    mode = "wb"

    while attempts < max_attempts:
        attempts += 1
        requested_offset = downloaded
        request_kwargs = dict(kwargs)
        if downloaded > 0 and range_ok:
            # If-Range 可防止远端内容变化后把不同版本拼进同一个文件。
            request_headers = dict(request_kwargs.get("headers") or {})
            request_headers["Range"] = f"bytes={downloaded}-"
            if validator:
                request_headers["If-Range"] = validator
            request_kwargs["headers"] = request_headers

        try:
            with client.stream(url, **request_kwargs) as response:
                status_code = response.status_code
                headers = dict(response.headers)

                if not (200 <= status_code < 300):
                    return ResumeResult(
                        path=target,
                        total_bytes=downloaded,
                        status_code=status_code,
                        attempts=attempts,
                        restarts=restarts,
                        headers=headers,
                    )

                if downloaded > 0 and status_code != 206:
                    log.info(f"{url} 无法从 {downloaded} 字节处续传（状态码 {status_code}），从头重下")
                    downloaded = 0
                    restarts += 1
                    mode = "wb"

                if requested_offset > 0 and status_code == 206:
                    # 任何不一致都必须在打开追加文件前拒绝，避免把错误区间写入磁盘。
                    expected = _validate_content_range(headers, requested_offset, expected, validator)

                if attempts == 1 or status_code == 200:
                    validator = _validator(headers)
                    range_ok = _supports_range(headers)
                    expected = response.content_length
                    if not range_ok:
                        log.debug(f"{url} 未声明 Accept-Ranges，中断后只能整体重下")

                with target.open(mode) as handle:
                    for chunk in response:
                        handle.write(chunk)
                        downloaded += len(chunk)
                        if chunk_callback is not None:
                            chunk_callback(downloaded, expected)
                mode = "ab"

                if response.trailer_error:
                    last_error = response.trailer_error
                    raise TransportError(response.trailer_error)

        except _InvalidRangeError:
            raise
        except TransportError as e:
            last_error = str(e)
            mode = "ab" if downloaded > 0 and range_ok else "wb"
            if not range_ok and downloaded > 0:
                downloaded = 0
                restarts += 1
            if attempts >= max_attempts:
                break
            sleep_for = _RETRY_BACKOFF * attempts
            log.warning(
                f"{url} 下载中断（已 {downloaded} 字节，第 {attempts}/{max_attempts} 次）：{e}，"
                f"{sleep_for:.1f} 秒后{'续传' if range_ok and downloaded else '重下'}"
            )
            time.sleep(sleep_for)
            continue

        if expected >= 0 and downloaded > expected:
            raise TransportError(f"响应体超出声明长度：已收到 {downloaded} / {expected} 字节")
        if expected >= 0 and downloaded < expected:
            last_error = f"响应体不完整：已收到 {downloaded} / {expected} 字节"
            if attempts >= max_attempts:
                break
            log.warning(f"{url} {last_error}，准备续传")
            mode = "ab" if range_ok else "wb"
            if not range_ok:
                downloaded = 0
                restarts += 1
            continue

        return ResumeResult(
            path=target,
            total_bytes=downloaded,
            status_code=status_code,
            attempts=attempts,
            restarts=restarts,
            headers=headers,
        )

    raise TransportError(
        f"{url} 在 {attempts} 次尝试后仍未下载完整（已 {downloaded} 字节）。最后一次错误：{last_error}"
    )


def iter_resumable(
    client: _Streamer,
    url: str,
    *,
    max_attempts: int = 5,
    **kwargs: Any,
) -> Iterator[bytes]:
    """逐块产出响应体，并在已产出数据可安全续传时自动恢复。"""
    if max_attempts < 1:
        raise ValidationError(f"max_attempts 必须 >= 1，当前为 {max_attempts}")

    downloaded = 0
    expected = -1
    validator: str | None = None
    range_ok = False
    attempts = 0
    last_error = ""
    fatal: TransportError | None = None

    while attempts < max_attempts:
        attempts += 1
        requested_offset = downloaded
        request_kwargs = dict(kwargs)
        if downloaded > 0:
            # 已 yield 的字节无法撤回，所以只有 206 才允许继续拼接。
            request_headers = dict(request_kwargs.get("headers") or {})
            request_headers["Range"] = f"bytes={downloaded}-"
            if validator:
                request_headers["If-Range"] = validator
            request_kwargs["headers"] = request_headers

        try:
            with client.stream(url, **request_kwargs) as response:
                if not (200 <= response.status_code < 300):
                    raise TransportError(f"{url} 返回不可下载的 HTTP 状态码 {response.status_code}")
                if downloaded > 0 and response.status_code != 206:
                    fatal = TransportError(
                        f"{url} 无法从 {downloaded} 字节处续传（状态码 {response.status_code}）；"
                        "已产出的分片无法收回，只能由调用方决定是否整体重来"
                    )
                    break

                if requested_offset > 0:
                    expected = _validate_content_range(dict(response.headers), requested_offset, expected, validator)

                if attempts == 1:
                    validator = _validator(dict(response.headers))
                    range_ok = _supports_range(dict(response.headers))
                    expected = response.content_length
                    if not range_ok:
                        log.debug(f"{url} 未声明 Accept-Ranges，中断后无法续传")

                for chunk in response:
                    downloaded += len(chunk)
                    yield chunk

                if response.trailer_error:
                    raise TransportError(response.trailer_error)
                if expected >= 0 and downloaded > expected:
                    raise _InvalidRangeError(f"响应体超出声明长度：已收到 {downloaded} / {expected} 字节")
                if expected >= 0 and downloaded < expected:
                    raise TransportError(f"响应体不完整：已收到 {downloaded} / {expected} 字节")
                return

        except _InvalidRangeError:
            raise
        except TransportError as e:
            last_error = str(e)
            if not range_ok or attempts >= max_attempts:
                break
            log.warning(f"{url} 流中断（已 {downloaded} 字节），准备续传：{e}")
            time.sleep(_RETRY_BACKOFF * attempts)

    if fatal is not None:
        raise fatal
    raise TransportError(f"{url} 在 {attempts} 次尝试后仍未读完。最后一次错误：{last_error}")


__all__ = ["ResumeResult", "download_to_file", "iter_resumable"]
