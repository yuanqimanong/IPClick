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
    for cut in range(4 if truncated else 1):
        chunk = sample[: len(sample) - cut] if cut else sample
        try:
            _ = chunk.decode("utf-8")
        except UnicodeDecodeError:
            continue
        return True
    return False


def choose(pb_request: Any, mode: str = "auto", threshold: int = DEFAULT_THRESHOLD) -> grpc.Compression | None:
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
