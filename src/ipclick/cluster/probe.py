from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, final

import grpc

from ipclick.auth import build_client_metadata
from ipclick.cluster.node import Node
from ipclick.cluster.tokens import token_for
from ipclick.dto.proto import task_pb2, task_pb2_grpc
from ipclick.health import check_health
from ipclick.sdk import CHANNEL_OPTIONS
from ipclick.tls import TLSSettings, channel_credentials, channel_options
from ipclick.utils.log_util import log


DEFAULT_PROBE_TIMEOUT = 5.0


@final
@dataclass(frozen=True)
class ProbeResult:
    node_id: str
    address: str
    reachable: bool
    authenticated: bool | None
    elapsed_ms: int
    remote_id: str = ""
    remote_version: str = ""
    remote_auth_required: bool | None = None
    remote_forward: bool | None = None
    remote_in_flight: int = 0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.reachable and self.authenticated is not False

    def snapshot(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "address": self.address,
            "ok": self.ok,
            "reachable": self.reachable,
            "authenticated": self.authenticated,
            "elapsed_ms": self.elapsed_ms,
            "remote_id": self.remote_id,
            "remote_version": self.remote_version,
            "remote_auth_required": self.remote_auth_required,
            "remote_forward": self.remote_forward,
            "remote_in_flight": self.remote_in_flight,
            "detail": self.detail,
        }


def probe_node(
    node: Node,
    *,
    secret: str = "",
    tls: TLSSettings | None = None,
    timeout: float = DEFAULT_PROBE_TIMEOUT,
    from_node: str = "",
) -> ProbeResult:
    settings = tls or TLSSettings()
    started = time.monotonic()

    healthy, health_detail = check_health(node.address, timeout=timeout, tls=settings)
    if not healthy:
        return ProbeResult(
            node_id=node.id,
            address=node.address,
            reachable=False,
            authenticated=None,
            elapsed_ms=_ms(started),
            detail=f"连不上：{health_detail}",
        )

    token = token_for(node.id, node.token, secret)
    if token is None:
        return ProbeResult(
            node_id=node.id,
            address=node.address,
            reachable=True,
            authenticated=None,
            elapsed_ms=_ms(started),
            detail="连得上。未配置集群内部鉴权，无令牌可验——任何能连到该端口的人都可以借它发请求",
        )

    return _ping(node, token, settings, timeout, started, from_node)


def _ping(
    node: Node,
    token: str,
    tls: TLSSettings,
    timeout: float,
    started: float,
    from_node: str,
) -> ProbeResult:
    target = node.address
    options = [*CHANNEL_OPTIONS, *channel_options(tls)]
    channel = (
        grpc.secure_channel(target, channel_credentials(tls), options=options)
        if tls.enabled
        else grpc.insecure_channel(target, options=options)
    )
    try:
        stub = task_pb2_grpc.TaskServiceStub(channel)
        response = stub.Ping(
            task_pb2.PingReq(from_node=from_node),
            timeout=timeout,
            metadata=build_client_metadata(token),
        )
    except grpc.RpcError as e:
        return _from_rpc_error(node, e, started)
    except Exception as e:
        log.debug(f"探测节点 {node.id} 时出现意外错误：{e}")
        return ProbeResult(
            node_id=node.id,
            address=node.address,
            reachable=True,
            authenticated=None,
            elapsed_ms=_ms(started),
            detail=f"探测出错：{type(e).__name__}: {e}",
        )
    finally:
        channel.close()

    detail = "连得上，集群内部鉴权通过"
    if not response.auth_required:
        detail = "连得上，但对方未启用鉴权：任何能连到该端口的人都可以借它发请求"
    if response.node_id and response.node_id != node.id:
        detail += f"。注意对方自报 id 是 {response.node_id!r}，与列表里的 {node.id!r} 不一致"

    return ProbeResult(
        node_id=node.id,
        address=node.address,
        reachable=True,
        authenticated=True,
        elapsed_ms=_ms(started),
        remote_id=response.node_id,
        remote_version=response.version,
        remote_auth_required=response.auth_required,
        remote_forward=response.forward,
        remote_in_flight=response.in_flight,
        detail=detail,
    )


def _from_rpc_error(node: Node, error: grpc.RpcError, started: float) -> ProbeResult:
    code = getattr(error, "code", lambda: None)()
    details = (getattr(error, "details", lambda: "")() or "").strip()

    if code is grpc.StatusCode.UNAUTHENTICATED:
        return ProbeResult(
            node_id=node.id,
            address=node.address,
            reachable=True,
            authenticated=False,
            elapsed_ms=_ms(started),
            detail=(
                "连得上，但鉴权不通过。两端的 IPCLICK_CLUSTER_SECRET 必须完全一致"
                "（在一台机器上生成，原样复制到其余机器的 .env）；"
                f"若该节点单独配了 token，请核对节点 id 是否写对（当前 {node.id!r}）。{details}"
            ),
        )

    if code is grpc.StatusCode.UNIMPLEMENTED:
        return ProbeResult(
            node_id=node.id,
            address=node.address,
            reachable=True,
            authenticated=True,
            elapsed_ms=_ms(started),
            detail="连得上、鉴权也通过，但对方版本低于 0.4（没有 Ping 接口），拿不到它的详细信息",
        )

    name = getattr(code, "name", str(code))
    return ProbeResult(
        node_id=node.id,
        address=node.address,
        reachable=code is not grpc.StatusCode.UNAVAILABLE,
        authenticated=None,
        elapsed_ms=_ms(started),
        detail=f"探测失败：{name}{f'：{details}' if details else ''}",
    )


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


__all__ = ["DEFAULT_PROBE_TIMEOUT", "ProbeResult", "probe_node"]
