"""gRPC 共享令牌鉴权。

IPClick 服务端会代替调用方请求任意 URL。在此之前它没有任何鉴权，
任何能连到端口的人都能拿它当代理用——SSRF 防护限制的是"能打到哪儿"，
不解决"谁能用"。这两件事不能互相替代。

传输方式采用 gRPC 标准做法：``authorization: Bearer <token>`` metadata 头，
任何语言的 gRPC 客户端都能对接。

令牌来源优先级：环境变量 ``IPCLICK_AUTH_TOKEN`` > 配置文件
``[SECURITY].auth_token``。密钥不该写进配置文件，环境变量是首选。
支持配置多个令牌以便轮换时新旧并存。

注：本模块使用 ``from __future__ import annotations``。grpc 的类型 stub 把
``RpcMethodHandler`` 声明成泛型，但运行时的类并不支持下标，直接写
``RpcMethodHandler[Any, Any]`` 会在 import 期抛 TypeError。延迟求值让类型
检查器能看到参数、运行时又不去真正求值。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import hmac
import os
from typing import Any

import grpc
from typing_extensions import override

from ipclick.trace import get_recorder
from ipclick.utils.log_util import log


#: metadata 中携带令牌的键。gRPC 要求 metadata 键为小写。
AUTH_METADATA_KEY = "authorization"

#: 令牌前缀，遵循 RFC 6750
BEARER_PREFIX = "Bearer "

#: 覆盖配置文件的环境变量
AUTH_TOKEN_ENV = "IPCLICK_AUTH_TOKEN"

#: 免鉴权的方法前缀。健康检查要供编排系统探活，通常拿不到密钥。
_EXEMPT_METHOD_PREFIXES: tuple[str, ...] = (
    "/grpc.health.v1.Health/",
    "/grpc.reflection.",
)


def load_tokens(security_config: dict[str, Any] | None = None) -> tuple[str, ...]:
    """收集所有有效令牌。

    Args:
        security_config: 配置文件的 ``[SECURITY]`` 节。

    Returns:
        去重后的令牌元组；为空表示未启用鉴权。
    """
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

    # 去重但保持顺序
    return tuple(dict.fromkeys(tokens))


def is_exempt(method: str) -> bool:
    """该 gRPC 方法是否免鉴权。"""
    return method.startswith(_EXEMPT_METHOD_PREFIXES)


def extract_token(metadata: Sequence[tuple[str, Any]] | None) -> str | None:
    """从 gRPC metadata 中取出令牌。

    同时接受带 ``Bearer `` 前缀和裸令牌两种写法——后者方便 grpcurl 之类的
    工具手动调试。
    """
    if not metadata:
        return None

    for key, value in metadata:
        if key.lower() != AUTH_METADATA_KEY:
            continue
        raw = (value.decode() if isinstance(value, bytes) else str(value)).strip()
        if not raw:
            return None

        # 按空白切成两段。不能先 strip 再判断 "Bearer " 前缀——"Bearer   "
        # 去掉尾部空白后就不含那个空格了，会被当成裸令牌返回字面量 "Bearer"。
        head, _, rest = raw.partition(" ")
        if head.lower() == BEARER_PREFIX.strip().lower():
            return rest.strip() or None
        return raw
    return None


def token_matches(candidate: str | None, valid_tokens: Sequence[str]) -> bool:
    """常量时间比较，避免通过响应耗时逐字节猜出令牌。

    注意必须遍历完所有令牌，不能命中就 break——提前返回会让"匹配第 1 个"
    和"匹配第 3 个"耗时不同，重新引入时序侧信道。
    """
    if not candidate:
        return False

    matched = False
    for token in valid_tokens:
        if hmac.compare_digest(candidate, token):
            matched = True
    return matched


def build_client_metadata(token: str | None) -> tuple[tuple[str, str], ...]:
    """构造客户端调用时附带的 metadata。token 为空则返回空元组。"""
    if not token:
        return ()
    return ((AUTH_METADATA_KEY, f"{BEARER_PREFIX}{token}"),)


class TokenAuthInterceptor(grpc.ServerInterceptor):
    """校验 ``authorization`` metadata 的服务端拦截器。

    令牌列表为空时直接放行——这样现有部署升级后不会立刻全部中断，
    但服务端启动时会打一条显著的告警。
    """

    def __init__(self, tokens: Sequence[str]):
        self._tokens: tuple[str, ...] = tuple(tokens)
        self._deny: grpc.RpcMethodHandler[Any, Any] = grpc.unary_unary_rpc_method_handler(self._reject)

    @property
    def enabled(self) -> bool:
        return bool(self._tokens)

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

        # 只记方法名，绝不记令牌本身（哪怕是错误的那个）
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
