from __future__ import annotations

import pytest

from ipclick.exceptions import ConfigError
from ipclick.utils.coerce import (
    as_bool,
    as_float,
    as_int,
    as_optional_text,
    as_positive_float,
    as_text,
    as_text_tuple,
    require_bool,
    require_float,
    require_int,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, False),
        (True, True),
        (False, False),
        ("true", True),
        (" ON ", True),
        ("1", True),
        ("false", False),
        ("", False),
        ("maybe", False),
        (1, True),
        (0, False),
        ([], False),
    ],
)
def test_as_bool(value: object, expected: bool) -> None:
    assert as_bool(value) is expected


def test_as_bool_keeps_the_default_for_unusable_input() -> None:
    assert as_bool(None, True) is True
    assert as_bool("nonsense", True) is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, 7), ("12", 12), (12.9, 12), ("x", 7), (True, 7), (False, 7), ([], 7)],
)
def test_as_int(value: object, expected: int) -> None:
    assert as_int(value, 7) == expected


def test_as_int_bounds_fall_back_instead_of_clamping() -> None:
    assert as_int(-1, 7, minimum=0) == 7
    assert as_int(0, 7, minimum=0) == 0
    assert as_int(70000, 7, maximum=65535) == 7


def test_as_float() -> None:
    assert as_float(None, 1.5) == 1.5
    assert as_float("2.5", 1.5) == 2.5
    assert as_float(True, 1.5) == 1.5
    assert as_float(-1.0, 1.5, minimum=0.0) == 1.5
    assert as_float(0.0, 1.5, minimum=0.0) == 0.0


def test_as_positive_float_rejects_zero() -> None:
    assert as_positive_float(0, 30.0) == 30.0
    assert as_positive_float(-2, 30.0) == 30.0
    assert as_positive_float(0.5, 30.0) == 0.5


def test_text_helpers() -> None:
    assert as_text(None, "fallback") == "fallback"
    assert as_text("  ", "fallback") == "fallback"
    assert as_text(" value ") == "value"
    assert as_text(9528) == "9528"
    assert as_optional_text(" ") is None
    assert as_optional_text(None) is None
    assert as_optional_text(" x ") == "x"


def test_as_text_tuple_normalises_and_filters() -> None:
    assert as_text_tuple(["A", " b ", "", 3]) == ("a", "b", "3")
    assert as_text_tuple("not a list") == ()
    assert as_text_tuple(None) == ()
    assert as_text_tuple(["image", "nope"], frozenset({"image"})) == ("image",)


def test_require_bool_is_loud_about_typos() -> None:
    assert require_bool(None, "SERVER.async_mode", True) is True
    assert require_bool("yes", "SERVER.async_mode") is True
    assert require_bool(0, "SERVER.async_mode") is False
    with pytest.raises(ConfigError, match=r"SERVER\.async_mode"):
        require_bool("maybe", "SERVER.async_mode")


def test_require_int_is_loud_about_typos() -> None:
    assert require_int(None, "f", 3) == 3
    assert require_int("4", "f", 3) == 4
    with pytest.raises(ConfigError, match="布尔值"):
        require_int(True, "f", 3)
    with pytest.raises(ConfigError, match="期望整数"):
        require_int("many", "f", 3)
    with pytest.raises(ConfigError, match="不能小于"):
        require_int(-1, "f", 3)


def test_require_float_is_loud_about_typos() -> None:
    assert require_float(None, "f", 1.0) == 1.0
    assert require_float("2", "f", 1.0) == 2.0
    with pytest.raises(ConfigError, match="布尔值"):
        require_float(False, "f", 1.0)
    with pytest.raises(ConfigError, match="期望数字"):
        require_float("fast", "f", 1.0)
    with pytest.raises(ConfigError, match="不能小于"):
        require_float(-0.5, "f", 1.0, minimum=0.0)
