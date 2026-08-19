from __future__ import annotations

import pytest

from ipclick.compression import DEFAULT_THRESHOLD, CompressionPolicy, normalize_mode


@pytest.mark.parametrize(("value", "expected"), [(True, "gzip"), (False, "none"), (1, "gzip"), (0, "none")])
def test_normalize_mode_accepts_toml_booleans_and_numeric_aliases(value: object, expected: str) -> None:
    assert normalize_mode(value) == expected


def test_non_finite_compression_threshold_falls_back_to_default() -> None:
    assert CompressionPolicy({"compression_threshold": float("inf")}).threshold == DEFAULT_THRESHOLD
