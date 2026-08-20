"""gRPC 令牌鉴权及客户端鉴权元数据工具。"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
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
    """按环境变量优先的顺序读取、去重服务端可接受的令牌。"""
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
    """判断 RPC 方法是否属于无需令牌的基础设施接口。"""
    return method.startswith(_EXEMPT_METHOD_PREFIXES)


def extract_token(metadata: Sequence[tuple[str, Any]] | None) -> str | None:
    """从 gRPC metadata 中提取裸令牌或 Bearer 令牌。"""
    if not metadata:
        return None

    for key, value in metadata:
        if key.lower() != AUTH_METADATA_KEY:
            continue
        try:
            raw = (value.decode() if isinstance(value, bytes) else str(value)).strip()
        except UnicodeDecodeError:
            # 非法字节属于无效凭据，不能让鉴权拦截器自身变成 INTERNAL。
            return None
        if not raw:
            return None

        head, _, rest = raw.partition(" ")
        if head.lower() == BEARER_PREFIX.strip().lower():
            return rest.strip() or None
        return raw
    return None


def token_matches(candidate: str | None, valid_tokens: Sequence[str]) -> bool:
    """以恒定时间比较候选令牌，避免泄露有效令牌前缀。"""
    if not candidate:
        return False

    # 不在首个命中处提前退出，使多令牌轮换时的比较路径保持稳定。
    matched = False
    for token in valid_tokens:
        if hmac.compare_digest(candidate, token):
            matched = True
    return matched


def build_client_metadata(token: str | None) -> tuple[tuple[str, str], ...]:
    """构造调用下游节点时使用的 Bearer metadata。"""
    if not token:
        return ()
    return ((AUTH_METADATA_KEY, f"{BEARER_PREFIX}{token}"),)


class TokenAuthInterceptor(grpc.ServerInterceptor):
    """对非豁免 RPC 执行令牌校验的同步 gRPC 拦截器。"""

    def __init__(self, tokens: Sequence[str]):
        self._tokens: tuple[str, ...] = tuple(tokens)
        self._deny: grpc.RpcMethodHandler[Any, Any] = grpc.unary_unary_rpc_method_handler(self._reject)

    @property
    def enabled(self) -> bool:
        """返回是否配置了至少一个有效令牌。"""
        return bool(self._tokens)

    @property
    def tokens(self) -> tuple[str, ...]:
        """返回当前接受的不可变令牌集合。"""
        return self._tokens

    def replace_tokens(self, tokens: Sequence[str]) -> None:
        """原子替换有效令牌，供集群身份或共享密钥热更新。"""
        self._tokens = tuple(tokens)

    @staticmethod
    def _reject(_request: Any, context: grpc.ServicerContext) -> Any:
        context.abort(grpc.StatusCode.UNAUTHENTICATED, "缺少或无效的鉴权令牌")

    @staticmethod
    def _reject_stream(_request: Any, context: grpc.ServicerContext) -> Iterator[Any]:
        # 空的 yield-from 让 gRPC 将本函数识别为流式 handler，随后立即中止调用。
        yield from ()
        context.abort(grpc.StatusCode.UNAUTHENTICATED, "缺少或无效的鉴权令牌")

    def _rejection_handler(self, handler: grpc.RpcMethodHandler[Any, Any]) -> grpc.RpcMethodHandler[Any, Any]:
        """创建与原 RPC 流式形态一致的拒绝 handler。"""
        kwargs = {
            "request_deserializer": handler.request_deserializer,
            "response_serializer": handler.response_serializer,
        }
        if handler.request_streaming:
            if handler.response_streaming:
                return grpc.stream_stream_rpc_method_handler(self._reject_stream, **kwargs)
            return grpc.stream_unary_rpc_method_handler(self._reject, **kwargs)
        if handler.response_streaming:
            return grpc.unary_stream_rpc_method_handler(self._reject_stream, **kwargs)
        return grpc.unary_unary_rpc_method_handler(self._reject, **kwargs)

    @override
    def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], grpc.RpcMethodHandler[Any, Any] | None],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler[Any, Any] | None:
        """放行有效调用，并以 UNAUTHENTICATED 拒绝其他调用。"""
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
        # 必须保留原 RPC 的 unary/stream 形态，否则流式调用会被 gRPC
        # 当成另一种方法类型执行，最终表现为 INTERNAL 而非 UNAUTHENTICATED。
        handler = continuation(handler_call_details)
        return self._rejection_handler(handler) if handler is not None else self._deny


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
