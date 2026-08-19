"""请求压缩策略。

自动化脚本是这里的主要动机：``automation_script`` 传的是整个脚本文件，几 KB 到
几十 KB 的纯文本，gzip 后通常只剩几百字节（实测 8158 → 350 字节，约 23 倍）。
一个爬虫任务批量发几百个带脚本的请求，压不压差出一个数量级的流量。

用 gRPC 自带的消息压缩，而不是在应用层自己 gzip 再 base64：

* 服务端**透明解压**，不需要在协议里加"这个字段压过没有"的标志位——那种标志位
  一旦和实际内容不一致就是解不开的乱码。
* 压的是**整条消息**，headers / cookies / json 一起受益，不只是脚本字段。
* 不改 protobuf，旧客户端与新服务端照常互通。

但不能无条件全开：

* 小请求（一个 URL 加几个 header，几百字节）压缩收益接近零，白搭 CPU。
* 已经压过的二进制体（图片、gzip 包、加密数据）再压一遍只会**变大**，
  同时吃掉可观的 CPU——上传 50MB 二进制时这不是小事。

所以默认 ``auto``：够大且看起来是可压缩内容才压。判定只看请求体的前若干字节，
是 O(1) 的。
"""

from __future__ import annotations

from typing import Any, Final, final

import grpc

from ipclick.utils.log_util import log


MODES: Final = frozenset({"auto", "gzip", "none"})

DEFAULT_THRESHOLD = 1024

_SAMPLE_SIZE = 512

_BINARY_RATIO = 0.02

_MAGIC_PREFIXES: Final = (
    b"\x1f\x8b",
    b"\x50\x4b\x03\x04",
    b"\x89PNG",
    b"\xff\xd8\xff",
    b"GIF8",
    b"RIFF",
    b"BZh",
    b"\xfd7zXZ",
    b"\x28\xb5\x2f\xfd",
    b"%PDF",
    b"\x00\x00\x00",
)

_TEXT_CONTROL = frozenset({0x09, 0x0A, 0x0C, 0x0D})


def normalize_mode(value: Any, default: str = "auto") -> str:
    """把配置里的取值归一成 :data:`MODES` 里的一个。

    不认识的取值退回默认并告警，不报错——压缩策略配错只影响流量，
    不该让客户端起不来。
    """
    mode = str(value or default).strip().lower()
    if mode in ("true", "yes", "1", "on"):
        return "gzip"
    if mode in ("false", "no", "0", "off"):
        return "none"
    if mode not in MODES:
        log.warning(f"未知的压缩模式 {mode!r}，改用 {default}。可选：{'、'.join(sorted(MODES))}")
        return default
    return mode


def looks_incompressible(body: bytes) -> bool:
    """这段字节看起来是不是已经压过 / 本来就是二进制。

    四道判定，从最确定的往下走：

    1. 已知格式的魔数（gzip / zip / png / jpeg …）——命中即确定。
    2. 采样里有 NUL —— 文本里几乎不可能出现。
    3. 采样**不是合法 UTF-8** —— 这一条是主力。JSON、表单、HTML、脚本全是
       合法 UTF-8；而 512 字节的随机数据能凑成合法 UTF-8 的概率约等于 0。
       只靠控制字符占比的话，随机数据的期望占比正好压在阈值上（见
       :data:`_BINARY_RATIO`），判定会变成掷硬币。
    4. 控制字符占比超标 —— 兜住"能解成 UTF-8 但明显是二进制"的少数情况。
    """
    if not body:
        return False
    for magic in _MAGIC_PREFIXES:
        if body.startswith(magic):
            return True
    sample = body[:_SAMPLE_SIZE]
    if b"\x00" in sample:
        return True
    if not _decodes_as_utf8(sample, truncated=len(body) > len(sample)):
        return True
    odd = sum(1 for byte in sample if byte < 0x20 and byte not in _TEXT_CONTROL)
    return odd / len(sample) > _BINARY_RATIO


def _decodes_as_utf8(sample: bytes, *, truncated: bool) -> bool:
    """采样能否解成 UTF-8。

    ``truncated`` 表示采样是从更长的数据里截出来的——那样最后一个多字节字符
    很可能被切断，不能因此就判成二进制。UTF-8 的字符最长 4 字节，所以往回退
    最多 3 个字节再试一次就够。
    """
    for cut in range(4 if truncated else 1):
        chunk = sample[: len(sample) - cut] if cut else sample
        try:
            _ = chunk.decode("utf-8")
        except UnicodeDecodeError:
            continue
        return True
    return False


def choose(pb_request: Any, mode: str = "auto", threshold: int = DEFAULT_THRESHOLD) -> grpc.Compression | None:
    """给一条请求选压缩方式。

    Args:
        pb_request: ``task_pb2.ReqTask``。只读 ``ByteSize()`` 与 ``data``。
        mode: 见 :data:`MODES`。
        threshold: ``auto`` 模式下的体积门槛。

    Returns:
        传给 gRPC 调用的 compression 参数；``None`` 表示按 channel 默认（不压）。
    """
    if mode == "none":
        return None
    if mode == "gzip":
        return grpc.Compression.Gzip

    try:
        size = int(pb_request.ByteSize())
    except Exception:  # pragma: no cover - 非 protobuf 对象
        return None
    if size < threshold:
        return None

    body: bytes = getattr(pb_request, "data", b"") or b""
    if body and len(body) * 2 > size and looks_incompressible(body):
        return None
    return grpc.Compression.Gzip


@final
class CompressionPolicy:
    """从 ``[CLIENT]`` 读出来的压缩策略，供客户端复用。"""

    __slots__ = ("mode", "threshold")

    def __init__(self, client_config: dict[str, Any] | None = None) -> None:
        config = dict(client_config or {})
        self.mode: str = normalize_mode(config.get("compression", "auto"))
        raw = config.get("compression_threshold", DEFAULT_THRESHOLD)
        try:
            self.threshold: int = max(0, int(raw))
        except (TypeError, ValueError):
            log.warning(f"[CLIENT].compression_threshold 不是整数（{raw!r}），改用 {DEFAULT_THRESHOLD}")
            self.threshold = DEFAULT_THRESHOLD

    def for_request(self, pb_request: Any) -> grpc.Compression | None:
        return choose(pb_request, self.mode, self.threshold)

    def for_stream(self) -> grpc.Compression | None:
        """流式（批量）整条流的压缩方式。

        流式只能在建流时定一次，没法逐条判定，所以 ``auto`` 在这里等于开——
        批量本身就是"量大"的场景，而且一条流里只要有一个请求值得压，
        整条流就值得压。
        """
        return None if self.mode == "none" else grpc.Compression.Gzip

    def describe(self) -> str:
        if self.mode == "auto":
            return f"auto（超过 {self.threshold} 字节且可压缩时用 gzip）"
        return self.mode


__all__ = [
    "DEFAULT_THRESHOLD",
    "MODES",
    "CompressionPolicy",
    "choose",
    "looks_incompressible",
    "normalize_mode",
]
