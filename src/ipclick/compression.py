"""为 unary gRPC 请求按内容选择压缩；流式 RPC 仅按开关决定是否 gzip。"""

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
    """把兼容的布尔写法归一为 ``auto``、``gzip`` 或 ``none``。"""
    raw = default if value is None or (isinstance(value, str) and not value.strip()) else value
    mode = str(raw).strip().lower()
    if mode in ("true", "yes", "1", "on"):
        return "gzip"
    if mode in ("false", "no", "0", "off"):
        return "none"
    if mode not in MODES:
        log.warning(f"未知的压缩模式 {mode!r}，改用 {default}。可选：{'、'.join(sorted(MODES))}")
        return default
    return mode


def looks_incompressible(body: bytes) -> bool:
    """用文件魔数和小样本启发式判断请求体是否不值得 gzip。"""
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
    """判断样本能否作为 UTF-8 解码，并容忍截断的末尾字符。"""
    for cut in range(4 if truncated else 1):
        chunk = sample[: len(sample) - cut] if cut else sample
        try:
            _ = chunk.decode("utf-8")
        except UnicodeDecodeError:
            continue
        return True
    return False


def choose(pb_request: Any, mode: str = "auto", threshold: int = DEFAULT_THRESHOLD) -> grpc.Compression | None:
    """根据模式、消息大小和请求体内容选择单次调用的 gRPC 压缩。"""
    if mode == "none":
        return None
    if mode == "gzip":
        return grpc.Compression.Gzip

    try:
        size = int(pb_request.ByteSize())
    except Exception:
        return None
    if size < threshold:
        return None

    body: bytes = getattr(pb_request, "data", b"") or b""
    if body and len(body) * 2 > size and looks_incompressible(body):
        return None
    return grpc.Compression.Gzip


@final
class CompressionPolicy:
    """从客户端配置构建可复用的请求压缩决策器。"""

    __slots__ = ("mode", "threshold")

    def __init__(self, client_config: dict[str, Any] | None = None) -> None:
        """解析压缩模式和启用压缩的字节阈值。"""
        config = dict(client_config or {})
        self.mode: str = normalize_mode(config.get("compression", "auto"))
        raw = config.get("compression_threshold", DEFAULT_THRESHOLD)
        try:
            self.threshold: int = max(0, int(raw))
        except (TypeError, ValueError, OverflowError):
            log.warning(f"[CLIENT].compression_threshold 不是整数（{raw!r}），改用 {DEFAULT_THRESHOLD}")
            self.threshold = DEFAULT_THRESHOLD

    def for_request(self, pb_request: Any) -> grpc.Compression | None:
        """返回普通请求应使用的压缩算法。"""
        return choose(pb_request, self.mode, self.threshold)

    def for_stream(self) -> grpc.Compression | None:
        """返回流式 RPC 的压缩算法；流无法在建流前估算整体大小。"""
        return None if self.mode == "none" else grpc.Compression.Gzip

    def describe(self) -> str:
        """返回适合展示在状态页上的策略说明。"""
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
