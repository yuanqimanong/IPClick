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
    mac = hmac.new(secret.encode("utf-8"), (_PURPOSE + node_id).encode("utf-8"), hashlib.sha256)
    return base64.urlsafe_b64encode(mac.digest()[:_TOKEN_BYTES]).decode("ascii").rstrip("=")


def cluster_secret(cluster_config: dict[str, Any] | None = None) -> str:
    env = os.getenv(CLUSTER_SECRET_ENV, "").strip()
    if env:
        return env
    return str((cluster_config or {}).get("secret") or "").strip()


def token_for(node_id: str, explicit: str = "", secret: str = "") -> str | None:
    if explicit.strip():
        return explicit.strip()
    if secret:
        return derive_token(secret, node_id)
    return None


def self_tokens(self_id: str, explicit: str = "", secret: str = "") -> tuple[str, ...]:
    tokens: list[str] = []
    if explicit.strip():
        tokens.append(explicit.strip())
    if secret and self_id:
        tokens.append(derive_token(secret, self_id))
    return tuple(dict.fromkeys(tokens))


def describe(self_id: str, explicit: str, secret: str) -> str:
    if explicit.strip():
        return f"节点 {self_id or '(未识别)'}：使用节点列表里显式配置的令牌"
    if secret:
        return f"节点 {self_id or '(未识别)'}：令牌由 {CLUSTER_SECRET_ENV} 派生"
    return "未配置集群内部鉴权（节点间调用不带令牌）"


def warn_if_missing(self_id: str, node_count: int, secret: str, explicit_count: int) -> None:
    if secret or explicit_count:
        return
    log.warning(
        f"已启用服务端转发（{node_count} 个节点）但未配置集群内部鉴权。"
        f"任何能连到这些端口的人都可以借本集群发请求。"
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
