from __future__ import annotations

import grpc
import pytest

from ipclick.compression import DEFAULT_THRESHOLD, CompressionPolicy, choose, normalize_mode
from ipclick.dto.proto import task_pb2


@pytest.mark.parametrize(("value", "expected"), [(True, "gzip"), (False, "none"), (1, "gzip"), (0, "none")])
def test_normalize_mode_accepts_toml_booleans_and_numeric_aliases(value: object, expected: str) -> None:
    assert normalize_mode(value) == expected


def test_non_finite_compression_threshold_falls_back_to_default() -> None:
    assert CompressionPolicy({"compression_threshold": float("inf")}).threshold == DEFAULT_THRESHOLD


@pytest.mark.parametrize("mode", ["none", "None", "NONE", " none ", "off", "false", "0", "No"])
def test_every_disabled_spelling_this_module_accepts_really_disables(mode: str) -> None:
    """``choose()`` 必须先归一化模式。

    原来它直接比字符串，于是本模块自己在 ``normalize_mode`` 里认可的那些"关闭"写法
    （大小写不同、带空格、off/false/0）全部落到 auto 分支上悄悄开了 gzip——明确
    把压缩关掉的人反而被压了，而且没有任何日志。
    """
    request = task_pb2.ReqTask(url="https://example.com/", data=b"a" * 4096)
    assert choose(request, mode=mode) is None


@pytest.mark.parametrize("mode", ["gzip", "GZIP", "true", "on", "1", "yes"])
def test_every_enabled_spelling_really_enables(mode: str) -> None:
    request = task_pb2.ReqTask(url="https://example.com/", data=b"a" * 4096)
    assert choose(request, mode=mode) is grpc.Compression.Gzip


def test_normalize_mode_never_advertises_an_invalid_fallback() -> None:
    """兜底值自己也必须是合法模式，否则会把非法值当"可选项"告诉用户。"""
    assert normalize_mode("bogus", default="also-bogus") == "auto"
    assert normalize_mode("bogus", default="none") == "none"
