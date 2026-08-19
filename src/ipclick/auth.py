from __future__ import annotations

from collections.abc import Callable, Sequence
import hmac
import os
from typing import Any

import grpc
from typing_extensions import override

from ipclick.trace import get_recorder
from ipclick.utils.log_util import log


AUTH_METADATA_KEY = "authorization"

BEARER_PREFIX = "Bearer "

AUTH_TOKEN_ENV = "IPCLICK_AUTH_TOKEN"

_EXEMPT_METHOD_PREFIXES: tuple[str, ...] = (
    "/grpc.health.v1.Health/",
    "/grpc.reflection.",
)


def load_tokens(security_config: dict[str, Any] | None = None) -> tuple[str, ...]:
    tokens: list[str] = []

    env_token = os.getenv(AUTH_TOKEN_ENV, "").strip()
    if env_token:
        tokens.append(env_token)

    configured = (security_config or {}).get("auth_token")
    if isinstance(configured, str):
        if configured.strip():
            tokens.append(configured.strip())
    elif isinstance(configured, (list, tuple)):
        tokens.extend(str(t).strip() for t in configured if str(t).strip())

    return tuple(dict.fromkeys(tokens))


def is_exempt(method: str) -> bool:
    return method.startswith(_EXEMPT_METHOD_PREFIXES)


def extract_token(metadata: Sequence[tuple[str, Any]] | None) -> str | None:
    if not metadata:
        return None

    for key, value in metadata:
        if key.lower() != AUTH_METADATA_KEY:
            continue
        raw = (value.decode() if isinstance(value, bytes) else str(value)).strip()
        if not raw:
            return None

        head, _, rest = raw.partition(" ")
        if head.lower() == BEARER_PREFIX.strip().lower():
            return rest.strip() or None
        return raw
    return None


def token_matches(candidate: str | None, valid_tokens: Sequence[str]) -> bool:
    if not candidate:
        return False

    matched = False
    for token in valid_tokens:
        if hmac.compare_digest(candidate, token):
            matched = True
    return matched


def build_client_metadata(token: str | None) -> tuple[tuple[str, str], ...]:
    if not token:
        return ()
    return ((AUTH_METADATA_KEY, f"{BEARER_PREFIX}{token}"),)


class TokenAuthInterceptor(grpc.ServerInterceptor):
    def __init__(self, tokens: Sequence[str]):
        self._tokens: tuple[str, ...] = tuple(tokens)
        self._deny: grpc.RpcMethodHandler[Any, Any] = grpc.unary_unary_rpc_method_handler(self._reject)

    @property
    def enabled(self) -> bool:
        return bool(self._tokens)

    @property
    def tokens(self) -> tuple[str, ...]:
        return self._tokens

    @staticmethod
    def _reject(_request: Any, context: grpc.ServicerContext) -> Any:
        context.abort(grpc.StatusCode.UNAUTHENTICATED, "缺少或无效的鉴权令牌")

    @override
    def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], grpc.RpcMethodHandler[Any, Any] | None],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler[Any, Any] | None:
        if not self._tokens:
            return continuation(handler_call_details)

        method: str = getattr(handler_call_details, "method", "") or ""
        if is_exempt(method):
            return continuation(handler_call_details)

        token = extract_token(getattr(handler_call_details, "invocation_metadata", None))
        if token_matches(token, self._tokens):
            return continuation(handler_call_details)

        log.warning(f"拒绝未通过鉴权的调用: {method}")
        get_recorder().record_rejected("unauthenticated")
        return self._deny


__all__ = [
    "AUTH_METADATA_KEY",
    "AUTH_TOKEN_ENV",
    "BEARER_PREFIX",
    "TokenAuthInterceptor",
    "build_client_metadata",
    "extract_token",
    "is_exempt",
    "load_tokens",
    "token_matches",
]
