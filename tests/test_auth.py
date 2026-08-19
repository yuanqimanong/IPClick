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
