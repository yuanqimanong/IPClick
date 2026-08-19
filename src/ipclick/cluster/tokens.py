"""集群共享密钥派生和节点令牌选择。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from typing import Any

from ipclick.utils.log_util import log


CLUSTER_SECRET_ENV = "IPCLICK_CLUSTER_SECRET"

_PURPOSE = "ipclick-node:"

_TOKEN_BYTES = 24


def derive_token(secret: str, node_id: str) -> str:
    """用 HMAC-SHA256 派生绑定节点 ID 的 URL-safe 令牌。"""
    mac = hmac.new(secret.encode("utf-8"), (_PURPOSE + node_id).encode("utf-8"), hashlib.sha256)
    return base64.urlsafe_b64encode(mac.digest()[:_TOKEN_BYTES]).decode("ascii").rstrip("=")


def cluster_secret(cluster_config: dict[str, Any] | None = None) -> str:
    """读取共享密钥，环境变量优先于配置文件。"""
    env = os.getenv(CLUSTER_SECRET_ENV, "").strip()
    if env:
        return env
    return str((cluster_config or {}).get("secret") or "").strip()


def token_for(node_id: str, explicit: str = "", secret: str = "") -> str | None:
    """选择节点显式令牌，否则由共享密钥派生。"""
    if explicit.strip():
        return explicit.strip()
    if secret:
        return derive_token(secret, node_id)
    return None


def self_tokens(self_id: str, explicit: str = "", secret: str = "") -> tuple[str, ...]:
    """生成服务端为自身节点接受的内部令牌集合。"""
    tokens: list[str] = []
    if explicit.strip():
        tokens.append(explicit.strip())
    if secret and self_id:
        tokens.append(derive_token(secret, self_id))
    return tuple(dict.fromkeys(tokens))


def describe(self_id: str, explicit: str, secret: str) -> str:
    """描述当前节点采用的内部鉴权令牌来源。"""
    if explicit.strip():
        return f"节点 {self_id or '(未识别)'}：使用节点列表里显式配置的令牌"
    if secret:
        return f"节点 {self_id or '(未识别)'}：令牌由 {CLUSTER_SECRET_ENV} 派生"
    return "未配置集群内部鉴权（节点间调用不带令牌）"


def warn_if_missing(self_id: str, node_count: int, secret: str, explicit_count: int) -> None:
    """服务端转发无任何内部凭据时发出安全警告。"""
    if secret or (node_count > 0 and explicit_count >= node_count):
        return
    missing = max(0, node_count - explicit_count)
    log.warning(
        f"已启用服务端转发，但有 {missing}/{node_count} 个节点未配置集群内部鉴权。"
        f"任何能连到这些未保护端口的人都可以借本集群发请求。"
        f"建议在所有节点的 .env 里设置同一个 {CLUSTER_SECRET_ENV}"
    )
    _ = self_id


__all__ = [
    "CLUSTER_SECRET_ENV",
    "cluster_secret",
    "derive_token",
    "describe",
    "self_tokens",
    "token_for",
    "warn_if_missing",
]
