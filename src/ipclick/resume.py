"""断点续传下载。

``Downloader.stream()`` 已经能把大文件分片拉下来，但**中途断了就得从头再来**。
大文件抓取里这是最疼的一件事：下到 90% 断线，前面 90% 全白费。

这里在流式通路之上实现 HTTP Range 续传：把已经落盘的字节数作为
``Range: bytes=N-`` 再发一次，服务端返回 206 就接着往后写。

拼接错版本的坑
--------------
天真的实现是"断了就带 Range 重来"。但如果目标文件在两次请求之间**变了**，
你会把新版本的后半段接到旧版本的前半段上——得到一个既不是旧版也不是新版、
而且**校验不出来**的文件。这比下载失败严重得多。

所以每次续传都带上 ``If-Range``（用首次响应的 ETag，没有就用 Last-Modified）：

* 资源没变 → 服务端回 **206**，安全地接着写；
* 资源变了 → 服务端回 **200** 并送来完整的新内容 → 丢掉已下载的部分重来。

服务端不支持 Range（没有 ``Accept-Ranges: bytes``，或对 Range 请求仍回 200）时，
自动退化成整体重下，不会悄悄产出损坏文件。
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Any, Protocol

from ipclick.exceptions import TransportError, ValidationError
from ipclick.utils.log_util import log


#: 单次续传之间的退避基数（秒）
_RETRY_BACKOFF = 1.0


class _Streamer(Protocol):
    """``Downloader`` / ``ClusterDownloader`` 中我们用到的那部分。"""

    def stream(self, url: str, **kwargs: Any) -> Any: ...


@dataclass
class ResumeResult:
    """一次续传下载的结果。"""

    path: Path
    total_bytes: int
    status_code: int
    #: 实际发起了几次请求。>1 说明中途断过并续传了
    attempts: int = 1
    #: 因为资源变化而放弃已下载内容、从头重来的次数
    restarts: int = 0
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def resumed(self) -> bool:
        return self.attempts > 1


def _validator(headers: dict[str, str]) -> str | None:
    """取用于 ``If-Range`` 的校验值。

    ETag 优先——它精确标识内容版本；Last-Modified 只有秒级精度，
    一秒内的两次修改分辨不出来，但聊胜于无。
    """
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
    """把 URL 流式下载到文件，中断后自动用 Range 续传。

    Args:
        client: :class:`~ipclick.sdk.Downloader` 或
            :class:`~ipclick.cluster.ClusterDownloader`。
        url: 目标地址。
        path: 落盘路径。已存在的文件会被覆盖（不会把新内容接到旧文件后面——
            那需要调用方自己确认那确实是同一个资源的前半段）。
        max_attempts: 总共最多发几次请求（含首次）。
        chunk_callback: ``(已下载字节, 总字节)``，总字节未知时为 -1。
        **kwargs: 透传给 ``client.stream()``，如 headers / proxy / timeout。

    Returns:
        ResumeResult

    Raises:
        ValidationError: max_attempts < 1。
        TransportError: 用尽次数仍未下载完整。
    """
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

    # "wb" 开一次，后续续传用 "ab" 追加。中途重来时重新 "wb" 截断。
    mode = "wb"

    while attempts < max_attempts:
        attempts += 1
        request_kwargs = dict(kwargs)
        if downloaded > 0 and range_ok:
            request_headers = dict(request_kwargs.get("headers") or {})
            request_headers["Range"] = f"bytes={downloaded}-"
            if validator:
                # 没有这一行，资源变了就会把新内容接到旧内容后面，
                # 拼出一个校验不出来的损坏文件
                request_headers["If-Range"] = validator
            request_kwargs["headers"] = request_headers

        try:
            with client.stream(url, **request_kwargs) as response:
                status_code = response.status_code
                headers = dict(response.headers)

                if not (200 <= status_code < 300):
                    # 4xx/5xx 不重试：换多少次也是同样的结果
                    return ResumeResult(
                        path=target,
                        total_bytes=downloaded,
                        status_code=status_code,
                        attempts=attempts,
                        restarts=restarts,
                        headers=headers,
                    )

                if downloaded > 0 and status_code != 206:
                    # 要么服务端不支持 Range，要么 If-Range 判定资源已变。
                    # 两种情况都必须丢掉已下载的部分，否则就是拼接两个版本。
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
                # 不支持 Range，重来必须从零开始
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

        # 走到这里说明这一轮读完了流且没有 trailer 错误
        if expected >= 0 and downloaded < expected:
            # 服务端声明了长度但给的字节数不够——流被截断了，当作中断处理
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
    """续传版的分片迭代器，不落盘。

    与 :func:`download_to_file` 同样的 Range / If-Range 语义，但把分片直接产出。
    中途重来（资源已变）时会抛 :class:`TransportError` 而不是静默重发——
    调用方已经消费掉的分片没法收回，继续下去只会让它拿到两个版本的混合体。
    """
    downloaded = 0
    validator: str | None = None
    range_ok = False
    attempts = 0
    last_error = ""
    #: 不可重试的失败。分片已经交给调用方了，收不回来——继续重发只会让它
    #: 拿到两个版本的混合体，所以这类错误必须原样上抛，不走下面的续传分支。
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
