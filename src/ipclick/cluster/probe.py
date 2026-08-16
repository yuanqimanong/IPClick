"""就地探测一个集群节点：连得上吗、鉴权配对吗。

要解决的是加完节点之后的那段空白：0.3 里节点填完只能等真实流量转发过去才发现
连不上，而那时错误已经混在业务失败里了。

**为什么不能只用 grpc.health.v1。** 健康检查刻意免鉴权（编排系统的探针通常拿不到
密钥）。于是在它眼里，"那台机器没起来"和"起来了但我的令牌不对"长得一模一样——
而这两件事的排查方向完全相反：前者去看进程和网络，后者去核对 ``.env`` 里的
``IPCLICK_CLUSTER_SECRET``。所以这里探两层：

1. **健康检查**（免鉴权）回答"连得上吗"。
2. **Ping**（走鉴权）回答"令牌对吗"。它不做任何业务动作——探测不该在对端产生
   一次真实抓取，那既慢又会污染对方的链路统计。

第二层的返回码就是结论：``OK`` 通过；``UNAUTHENTICATED`` 令牌不匹配；
``UNIMPLEMENTED`` 说明拦截器已经放行、只是对端还是 0.3（那个版本没有 Ping）。
最后这一种恰恰证明鉴权是通的，得单独说，否则滚动升级期间会被误报成鉴权失败。
"""

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


#: 单次探测的超时（秒）。探测是人点了按钮在等的动作，宁可早点报"连不上"
#: 也不要让页面转十几秒——真连得上的节点从来不需要这么久。
DEFAULT_PROBE_TIMEOUT = 5.0


@final
@dataclass(frozen=True)
class ProbeResult:
    """一次节点探测的结论。

    ``reachable`` 与 ``authenticated`` 分开，因为它们指向完全不同的排查方向。
    """

    node_id: str
    address: str
    #: 端口连得上、且对端在跑 IPClick
    reachable: bool
    #: 集群内部鉴权是否配对。None = 没验（连都没连上，或本地压根没配密钥）
    authenticated: bool | None
    elapsed_ms: int
    #: 对端**自报**的 id。和节点列表里写的对不上是个真问题——转发的路由、
    #: 链路记录里的"谁执行的"都以列表里那个为准，对不上就查不下去了。
    remote_id: str = ""
    remote_version: str = ""
    #: 对端有没有启用鉴权。False = 任何能连到它端口的人都能借它发请求。
    remote_auth_required: bool | None = None
    remote_forward: bool | None = None
    remote_in_flight: int = 0
    #: 给人看的一句话结论
    detail: str = ""

    @property
    def ok(self) -> bool:
        """整体算不算通过。鉴权没验过（本地没配密钥）不算失败——
        内网全互信的部署是合法选择。
        """
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
    """探一个节点。绝不抛异常——这是个诊断入口，任何失败都该变成可读的结论。"""
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
        # 本地没配共享密钥、节点也没写 token —— 没有令牌可验，只能报到这一步。
        # 这不是失败：不开集群内部鉴权是合法选择（但值得说一句）。
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
    # 复用客户端那份 channel 选项：里面有 enable_http_proxy=0。不关掉的话
    # gRPC 会读环境里的 http_proxy，把内网探测劫到代理上去——开发机上普遍设了
    # http_proxy，症状是探测全部 UNAVAILABLE 而节点其实好好的。
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
    except Exception as e:  # pragma: no cover - 兜底，诊断入口不该抛
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
        # 探测成功本身分不清"我的令牌对"和"它根本不验"，所以对端要自报这一位
        detail = "连得上，但对方未启用鉴权：任何能连到该端口的人都可以借它发请求"
    if response.node_id and response.node_id != node.id:
        # 转发的路由与链路记录里的"谁执行的"都以节点列表里的 id 为准，
        # 对不上会让两边的记录拼不到一起
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
        # 能走到方法查找这一步，说明鉴权拦截器已经放行了——这条恰恰证明令牌是对的。
        # 滚动升级期间必须单独说，否则会被误报成鉴权失败。
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
        # 健康检查刚过就 UNAVAILABLE，多半是探测这一跳撞上了别的问题（TLS、代理）
        reachable=code is not grpc.StatusCode.UNAVAILABLE,
        authenticated=None,
        elapsed_ms=_ms(started),
        detail=f"探测失败：{name}{f'：{details}' if details else ''}",
    )


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


__all__ = ["DEFAULT_PROBE_TIMEOUT", "ProbeResult", "probe_node"]
