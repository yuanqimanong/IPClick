"""gRPC 标准健康检查（``grpc.health.v1``）。

用标准协议而不是自定义 RPC，是为了让 Kubernetes 的 gRPC 探针、grpc_health_probe、
以及各类服务网格开箱即用。P4 的集群节点探活也会复用这套。

健康状态的语义：

* ``SERVING``     —— 可以接收流量
* ``NOT_SERVING`` —— 暂时不要给我发流量（如正在优雅停机）
* ``SERVICE_UNKNOWN`` —— 查询了一个本进程没有注册的服务名

停机时先把状态置为 ``NOT_SERVING`` 再真正 stop：负载均衡器据此把节点摘掉，
在途请求还能在优雅期内跑完，而不是先掐连接再让上游发现。
"""

from __future__ import annotations

from concurrent import futures
from typing import Any

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from ipclick.tls import TLSSettings, channel_credentials, channel_options
from ipclick.utils.log_util import log


#: gRPC 约定：空字符串代表"整个服务器"的总体健康状态。
#: kubelet 的 gRPC 探针默认查的就是它。
OVERALL_SERVICE = ""

#: 本项目具体服务的全限定名，与 task.proto 中的 package + service 一致。
TASK_SERVICE_NAME = "task.TaskService"

SERVING = health_pb2.HealthCheckResponse.SERVING
NOT_SERVING = health_pb2.HealthCheckResponse.NOT_SERVING


class HealthReporter:
    """包一层 grpc 官方的 HealthServicer，统一维护本项目关心的几个服务名。"""

    def __init__(self, enabled: bool = True, max_workers: int = 2):
        self.enabled: bool = enabled
        # HealthServicer 的 Watch 是流式的，需要自己的线程池，不能占用
        # 业务请求的 worker——否则一堆 watcher 就能把业务线程池占满。
        self._servicer: health.HealthServicer | None = (
            health.HealthServicer(experimental_thread_pool=futures.ThreadPoolExecutor(max_workers=max_workers))
            if enabled
            else None
        )

    @property
    def servicer(self) -> health.HealthServicer | None:
        return self._servicer

    def register(self, server: grpc.Server) -> None:
        """把健康检查服务注册到 gRPC 服务器上。"""
        if self._servicer is None:
            log.info("健康检查未启用（[MONITOR].health_check = false）")
            return
        health_pb2_grpc.add_HealthServicer_to_server(self._servicer, server)
        log.debug("已注册 grpc.health.v1 健康检查服务")

    def set_serving(self) -> None:
        """标记为可接收流量。"""
        self._set(SERVING)

    def set_not_serving(self) -> None:
        """标记为不再接收新流量（优雅停机的第一步）。"""
        self._set(NOT_SERVING)

    def _set(self, status: int) -> None:
        if self._servicer is None:
            return
        for service in (OVERALL_SERVICE, TASK_SERVICE_NAME):
            self._servicer.set(service, status)
        name = health_pb2.HealthCheckResponse.ServingStatus.Name(status)
        log.debug(f"健康状态 -> {name}")

    def enter_graceful_shutdown(self) -> None:
        """进入优雅停机：先摘流量再停服务。

        官方 servicer 提供的 enter_graceful_shutdown() 会把所有服务置为
        NOT_SERVING 并拒绝后续状态变更，正是我们想要的语义。
        """
        if self._servicer is None:
            return
        self._servicer.enter_graceful_shutdown()
        log.info("健康检查已置为 NOT_SERVING，负载均衡器可据此摘除本节点")


def check_health(
    target: str,
    *,
    service: str = OVERALL_SERVICE,
    timeout: float = 5.0,
    tls: TLSSettings | None = None,
) -> tuple[bool, str]:
    """以客户端身份查询某个 IPClick 服务端的健康状态。

    健康检查不需要鉴权，因此这里不带令牌——供 CLI、Docker healthcheck
    以及 P4 的集群节点探活复用。

    Args:
        target: ``host:port``
        service: 要查询的服务名，默认查总体状态
        timeout: 超时（秒）
        tls: 目标服务端的 TLS 配置。服务端开了 TLS 而这里还用明文连，
            探活会一直失败——集群会把健康节点全判成挂了。

    Returns:
        ``(是否健康, 状态描述)``
    """
    try:
        # enable_http_proxy=0：gRPC 也会读环境里的 http_proxy，不关掉的话
        # 探本机节点会被路由到环境代理去。
        settings = tls or TLSSettings()
        options: list[tuple[str, Any]] = [("grpc.enable_http_proxy", 0), *channel_options(settings)]
        channel_ctx = (
            grpc.secure_channel(target, channel_credentials(settings), options=options)
            if settings.enabled
            else grpc.insecure_channel(target, options=options)
        )
        with channel_ctx as channel:
            stub = health_pb2_grpc.HealthStub(channel)
            # grpc_health 没有随包发 health_pb2_grpc.pyi，且 Check 是在 __init__ 里
            # 动态赋值的，类型检查器看不到——运行时是存在的。
            response = stub.Check(  # pyright: ignore[reportAttributeAccessIssue]
                health_pb2.HealthCheckRequest(service=service), timeout=timeout
            )
    except grpc.RpcError as e:
        code = e.code() if hasattr(e, "code") else None
        details = e.details() if hasattr(e, "details") else str(e)
        return False, f"{code}: {details}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

    status_name = health_pb2.HealthCheckResponse.ServingStatus.Name(response.status)
    return response.status == SERVING, status_name


__all__ = [
    "NOT_SERVING",
    "OVERALL_SERVICE",
    "SERVING",
    "TASK_SERVICE_NAME",
    "HealthReporter",
    "check_health",
]
