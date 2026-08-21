from __future__ import annotations

import pytest

from ipclick.auth import (
    AUTH_METADATA_KEY,
    AUTH_TOKEN_ENV,
    build_client_metadata,
    extract_token,
    is_exempt,
    load_tokens,
    token_matches,
)


def test_load_tokens_reads_env_first_then_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(AUTH_TOKEN_ENV, " from-env ")
    assert load_tokens({"auth_token": "from-config"}) == ("from-env", "from-config")


def test_load_tokens_accepts_a_list_and_dedupes() -> None:
    assert load_tokens({"auth_token": ["a", " a ", "b", "  "]}) == ("a", "b")


def test_load_tokens_is_empty_without_configuration() -> None:
    assert load_tokens({}) == ()
    assert load_tokens(None) == ()


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ((("authorization", "Bearer tok"),), "tok"),
        ((("Authorization", "bearer tok"),), "tok"),
        ((("authorization", "tok"),), "tok"),
        ((("authorization", b"Bearer tok"),), "tok"),
        ((("authorization", "  "),), None),
        ((("authorization", "Bearer   "),), None),
        ((("other", "tok"),), None),
        ((), None),
        (None, None),
    ],
)
def test_extract_token(metadata: tuple[tuple[str, object], ...] | None, expected: str | None) -> None:
    assert extract_token(metadata) == expected


def test_token_matches() -> None:
    assert token_matches("a", ["b", "a"]) is True
    assert token_matches("a", ["b"]) is False
    assert token_matches(None, ["a"]) is False
    assert token_matches("a", []) is False


def test_build_client_metadata() -> None:
    assert build_client_metadata("tok") == ((AUTH_METADATA_KEY, "Bearer tok"),)
    assert build_client_metadata(None) == ()


def test_health_and_reflection_are_exempt() -> None:
    assert is_exempt("/grpc.health.v1.Health/Check") is True
    assert is_exempt("/grpc.reflection.v1alpha.ServerReflection/Info") is True
    assert is_exempt("/task.TaskService/Send") is False


@pytest.mark.parametrize(
    ("candidate", "valid", "expected"),
    [
        ("令牌abc", ["令牌abc"], True),
        ("令牌abc", ["令牌xyz"], False),
        ("t🔑", ["t🔑"], True),
        ("abc", ["令牌"], False),
        ("令牌", ["abc"], False),
        ("a", ["abcdef"], False),
    ],
)
def test_non_ascii_tokens_compare_without_crashing(candidate: str, valid: list[str], expected: bool) -> None:
    """非 ASCII 令牌必须能正常比对，而不是让整个鉴权拦截器抛 TypeError。

    hmac.compare_digest 对含非 ASCII 字符的 str 直接抛 TypeError。令牌是人从文档或
    聊天窗口粘过来的，混进全角字符完全可能——而那会让**所有**请求（包括带正确令牌的）
    都失败，同时启动日志仍显示鉴权已启用。
    """
    assert token_matches(candidate, valid) is expected
