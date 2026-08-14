"""请求压缩策略。"""

from __future__ import annotations

import grpc
import pytest

from ipclick.compression import (
    DEFAULT_THRESHOLD,
    CompressionPolicy,
    choose,
    looks_incompressible,
    normalize_mode,
)
from ipclick.dto.models import DownloadTask


def _task(**kwargs: object):
    return DownloadTask(url="http://example.com/x", **kwargs).to_protobuf()  # pyright: ignore[reportArgumentType]


class TestNormalizeMode:
    @pytest.mark.parametrize("value", ["auto", "AUTO", " auto "])
    def test_auto(self, value: str):
        assert normalize_mode(value) == "auto"

    @pytest.mark.parametrize("value", [True, "true", "on", "1", "gzip"])
    def test_truthy_means_gzip(self, value: object):
        assert normalize_mode(value) == "gzip"

    @pytest.mark.parametrize("value", ["false", "off", "0", "none"])
    def test_falsy_means_none(self, value: str):
        assert normalize_mode(value) == "none"

    def test_unknown_falls_back_without_raising(self):
        """压缩策略配错只影响流量，不该让客户端起不来。"""
        assert normalize_mode("brotli") == "auto"


class TestChoose:
    def test_small_request_not_compressed(self):
        assert choose(_task()) is None

    def test_large_script_is_compressed(self):
        """自动化脚本是这套机制的主要动机。"""
        pb = _task(automation_script="await page.goto(url)\n" * 500)
        assert pb.ByteSize() > DEFAULT_THRESHOLD
        assert choose(pb) is grpc.Compression.Gzip

    def test_large_text_body_is_compressed(self):
        assert choose(_task(data="a=1&" * 2000)) is grpc.Compression.Gzip

    def test_large_binary_body_is_skipped(self):
        """已经压过的东西再压只会变大还费 CPU。"""
        gzipped = b"\x1f\x8b\x08\x00" + bytes(range(256)) * 40
        assert choose(_task(data=gzipped)) is None

    def test_random_binary_body_is_skipped(self):
        import os

        assert choose(_task(data=os.urandom(20_000))) is None

    def test_random_binary_detection_is_not_a_coin_flip(self):
        """回归：判定阈值曾经是 10%，而随机字节里控制字符的期望占比是 10.9% ——
        正好压在线上，实测 400 次里约 5% 判错。随机/加密数据恰恰是最不该压的。

        跑 200 次而不是 1 次：这种"大部分时候对"的缺陷只跑一次是抓不到的
        （当初就是全量测试偶发失败才暴露出来）。
        """
        import os

        misses = [i for i in range(200) if not looks_incompressible(os.urandom(4096))]
        assert misses == [], f"{len(misses)}/200 次把随机数据判成了可压缩"

    def test_mode_gzip_compresses_even_tiny(self):
        assert choose(_task(), mode="gzip") is grpc.Compression.Gzip

    def test_mode_none_never_compresses(self):
        assert choose(_task(automation_script="x" * 100_000), mode="none") is None

    def test_threshold_respected(self):
        pb = _task(automation_script="y" * 2000)
        assert choose(pb, threshold=1_000_000) is None
        assert choose(pb, threshold=10) is grpc.Compression.Gzip

    def test_small_binary_body_with_large_text_still_compresses(self):
        """请求体不是体积主因时，别因为它是二进制就放弃整条消息的压缩。"""
        pb = _task(data=b"\x00\x01\x02\x03", automation_script="await page.goto(url)\n" * 500)
        assert choose(pb) is grpc.Compression.Gzip


class TestLooksIncompressible:
    @pytest.mark.parametrize(
        "body",
        [
            b"\x1f\x8b\x08\x00rest",
            b"PK\x03\x04rest",
            b"\x89PNG\r\n",
            b"\xff\xd8\xffJFIF",
            b"%PDF-1.7",
        ],
    )
    def test_magic_numbers(self, body: bytes):
        assert looks_incompressible(body) is True

    @pytest.mark.parametrize(
        "body",
        [
            b"",
            b"a=1&b=2",
            '{"key": "值"}'.encode(),
            b"await page.goto(url)\nawait page.click('#x')\n",
        ],
    )
    def test_text_is_compressible(self, body: bytes):
        assert looks_incompressible(body) is False

    def test_nul_byte_means_binary(self):
        assert looks_incompressible(b"text\x00more") is True

    def test_invalid_utf8_means_binary(self):
        """主力判据：合法 UTF-8 才当文本。随机 512 字节凑成合法 UTF-8 的概率约等于 0。"""
        assert looks_incompressible(b"\xff\xfe\xfd\xfc" * 200) is True

    def test_truncated_multibyte_char_is_not_binary(self):
        """采样是从更长的数据里截出来的，最后一个多字节字符很可能被切断——
        不能因此把一段中文判成二进制。"""
        assert looks_incompressible("世界你好".encode() * 400) is False

    def test_latin1_text_is_not_flagged_by_the_control_char_rule(self):
        """latin-1 的高位字节 >= 0x80，不是控制字符；但它不是合法 UTF-8，
        所以会走"非 UTF-8 -> 二进制"这一条。这是对的：非 UTF-8 的字节流
        本来就该按二进制原样送，压不压都不该猜。"""
        assert looks_incompressible("café".encode("latin-1") * 300) is True


class TestPolicy:
    def test_defaults(self):
        policy = CompressionPolicy({})
        assert policy.mode == "auto"
        assert policy.threshold == DEFAULT_THRESHOLD

    def test_from_config(self):
        policy = CompressionPolicy({"compression": "gzip", "compression_threshold": 4096})
        assert (policy.mode, policy.threshold) == ("gzip", 4096)

    def test_garbage_threshold_falls_back(self):
        assert CompressionPolicy({"compression_threshold": "big"}).threshold == DEFAULT_THRESHOLD

    def test_stream_compresses_on_auto(self):
        """流式只能在建流时定一次，auto 在那里等于开。"""
        assert CompressionPolicy({}).for_stream() is grpc.Compression.Gzip
        assert CompressionPolicy({"compression": "none"}).for_stream() is None

    def test_describe_mentions_threshold(self):
        assert "1024" in CompressionPolicy({}).describe()
