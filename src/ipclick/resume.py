from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Any, Protocol

from ipclick.exceptions import TransportError, ValidationError
from ipclick.protocols import StreamedBody
from ipclick.utils.log_util import log


_RETRY_BACKOFF = 1.0


class _Streamer(Protocol):
    def stream(self, url: str, **kwargs: Any) -> StreamedBody: ...


@dataclass
class ResumeResult:
    path: Path
    total_bytes: int
    status_code: int
    attempts: int = 1
    restarts: int = 0
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def resumed(self) -> bool:
        return self.attempts > 1


def _validator(headers: dict[str, str]) -> str | None:
    lowered = {k.lower(): v for k, v in headers.items()}
    return lowered.get("etag") or lowered.get("last-modified")


def _supports_range(headers: dict[str, str]) -> bool:
    lowered = {k.lower(): v for k, v in headers.items()}
    return "bytes" in lowered.get("accept-ranges", "").lower()


def download_to_file(
    client: _Streamer,
    url: str,
    path: str | Path,
    *,
    max_attempts: int = 5,
    chunk_callback: Callable[[int, int], None] | None = None,
    **kwargs: Any,
) -> ResumeResult:
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
        request_kwargs = dict(kwargs)
        if downloaded > 0 and range_ok:
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
    downloaded = 0
    validator: str | None = None
    range_ok = False
    attempts = 0
    last_error = ""
    fatal: TransportError | None = None

    while attempts < max_attempts:
        attempts += 1
        request_kwargs = dict(kwargs)
        if downloaded > 0:
            request_headers = dict(request_kwargs.get("headers") or {})
            request_headers["Range"] = f"bytes={downloaded}-"
            if validator:
                request_headers["If-Range"] = validator
            request_kwargs["headers"] = request_headers

        try:
            with client.stream(url, **request_kwargs) as response:
                if downloaded > 0 and response.status_code != 206:
                    fatal = TransportError(
                        f"{url} 无法从 {downloaded} 字节处续传（状态码 {response.status_code}）；"
                        "已产出的分片无法收回，只能由调用方决定是否整体重来"
                    )
                    break

                if attempts == 1:
                    validator = _validator(dict(response.headers))
                    range_ok = _supports_range(dict(response.headers))
                    if not range_ok:
                        log.debug(f"{url} 未声明 Accept-Ranges，中断后无法续传")

                for chunk in response:
                    downloaded += len(chunk)
                    yield chunk

                if response.trailer_error:
                    raise TransportError(response.trailer_error)
                return

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
