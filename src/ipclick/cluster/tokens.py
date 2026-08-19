"""集群内部鉴权：一个共享密钥，派生出每个节点各自的令牌。

要解决的问题是"节点间怎么互相鉴权"。最直觉的做法是给每台机器手写一个令牌、
再在每台机器的节点列表里把其他机器的令牌抄一遍——5 台机器就是 20 份抄写，
加一台要改 6 个文件，而且改漏一处的症状是运行时 UNAUTHENTICATED，很难定位。

这里改成派生：

    node_token = base64url( HMAC-SHA256(cluster_secret, "ipclick-node:" + node_id) )[:32]

* 每台机器只需要**同一个** ``IPCLICK_CLUSTER_SECRET``（放 ``.env``）。
* 每个节点用自己的 ``self_id`` 算出自己该接受哪个令牌。
* 转发方用目标的 ``node_id`` 算出该带哪个令牌。两边算的必然一致。
* 令牌**各不相同**：拿到子节点 B 的令牌不等于能调 C，也不等于拿到共享密钥
  （HMAC 单向）。这是"每节点独立令牌"和"全集群一个口令"的差别所在。
* 加一台机器只需要在节点列表里加一行，不需要发放任何新凭据。

节点条目里显式写的 ``token`` 优先——用于对接已有的、令牌不由这里管理的节点。
"""

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
    """由共享密钥与节点 id 派生该节点的令牌。"""
    mac = hmac.new(secret.encode("utf-8"), (_PURPOSE + node_id).encode("utf-8"), hashlib.sha256)
    return base64.urlsafe_b64encode(mac.digest()[:_TOKEN_BYTES]).decode("ascii").rstrip("=")


def cluster_secret(cluster_config: dict[str, Any] | None = None) -> str:
    """取集群共享密钥。环境变量优先于配置文件。

    密钥是机密，正规位置是 ``.env`` 里的 ``IPCLICK_CLUSTER_SECRET``；
    写在 ``ipclick.toml`` 的 ``[CLUSTER].secret`` 里也能用，但启动时会告警
    （统一由 :mod:`ipclick.secrets` 处理）。
    """
    env = os.getenv(CLUSTER_SECRET_ENV, "").strip()
    if env:
        return env
    return str((cluster_config or {}).get("secret") or "").strip()


def token_for(node_id: str, explicit: str = "", secret: str = "") -> str | None:
    """某个节点该用哪个令牌调用。

    Args:
        node_id: 目标节点 id。
        explicit: 节点条目里显式写的 token（优先）。
        secret: 集群共享密钥。

    Returns:
        令牌；两者都没有则返回 None（表示目标节点未启用鉴权）。
    """
    if explicit.strip():
        return explicit.strip()
    if secret:
        return derive_token(secret, node_id)
    return None


def self_tokens(self_id: str, explicit: str = "", secret: str = "") -> tuple[str, ...]:
    """本节点应当**接受**的集群内部令牌。

    与 :func:`token_for` 对称：转发方按目标 id 算，本节点按自己的 id 算，
    两边必然得出同一个值。返回元组是因为显式令牌与派生令牌可以并存
    （滚动更换密钥时两者都得先认一段时间）。
    """
    tokens: list[str] = []
    if explicit.strip():
        tokens.append(explicit.strip())
    if secret and self_id:
        tokens.append(derive_token(secret, self_id))
    return tuple(dict.fromkeys(tokens))


def describe(self_id: str, explicit: str, secret: str) -> str:
    """给启动日志与 config-info 用的一句话说明。"""
    if explicit.strip():
        return f"节点 {self_id or '(未识别)'}：使用节点列表里显式配置的令牌"
    if secret:
        return f"节点 {self_id or '(未识别)'}：令牌由 {CLUSTER_SECRET_ENV} 派生"
    return "未配置集群内部鉴权（节点间调用不带令牌）"


def warn_if_missing(self_id: str, node_count: int, secret: str, explicit_count: int) -> None:
    """转发开着但没有任何内部鉴权时提醒一句。

    不报错：内网全互信的部署是合法选择，而且服务端本身可以完全不开鉴权。
    但这件事必须让人看见——它意味着任何能连到节点端口的人都能借它发请求。
    """
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
