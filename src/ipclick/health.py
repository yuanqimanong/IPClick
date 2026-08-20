"""标准 gRPC 健康服务注册、状态切换和远端探测。"""

from __future__ import annotations

from concurrent import futures
from typing import Any

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from ipclick.rpc import open_channel_for
from ipclick.tls import TLSSettings
from ipclick.utils.log_util import log


OVERALL_SERVICE = ""

TASK_SERVICE_NAME = "task.TaskService"

SERVING = health_pb2.HealthCheckResponse.SERVING
NOT_SERVING = health_pb2.HealthCheckResponse.NOT_SERVING


class HealthReporter:
    """管理同步 gRPC 健康服务及其专用线程池。"""

    def __init__(self, enabled: bool = True, max_workers: int = 2):
        self.enabled: bool = enabled
        self._executor: futures.ThreadPoolExecutor | None = (
            futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ipclick-health")
            if enabled
            else None
        )
        self._servicer: health.HealthServicer | None = (
            health.HealthServicer(experimental_thread_pool=self._executor) if enabled else None
        )

    @property
    def servicer(self) -> health.HealthServicer | None:
        """返回底层健康服务实现，禁用时为 ``None``。"""
        return self._servicer

    def register(self, server: grpc.Server) -> None:
        """将标准健康服务注册到 server；禁用时保持空操作。"""
        if self._servicer is None:
            log.info("健康检查未启用（[MONITOR].health_check = false）")
            return
        health_pb2_grpc.add_HealthServicer_to_server(self._servicer, server)
        log.debug("已注册 grpc.health.v1 健康检查服务")

    def set_serving(self) -> None:
        """将整体与任务服务同时标记为可服务。"""
        self._set(SERVING)

    def set_not_serving(self) -> None:
        """将整体与任务服务同时标记为不可服务。"""
        self._set(NOT_SERVING)

    def _set(self, status: int) -> None:
        if self._servicer is None:
            return
        for service in (OVERALL_SERVICE, TASK_SERVICE_NAME):
            self._servicer.set(service, status)
        name = health_pb2.HealthCheckResponse.ServingStatus.Name(status)
        log.debug(f"健康状态 -> {name}")

    def enter_graceful_shutdown(self) -> None:
        """进入不可逆的优雅停机状态，使负载均衡器摘除本节点。"""
        if self._servicer is None:
            return
        self._servicer.enter_graceful_shutdown()
        log.info("健康检查已置为 NOT_SERVING，负载均衡器可据此摘除本节点")

    def close(self) -> None:
        """关闭健康检查专用线程池；可重复调用。"""
        executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)


def check_health(
    target: str,
    *,
    service: str = OVERALL_SERVICE,
    timeout: float = 5.0,
    tls: TLSSettings | None = None,
) -> tuple[bool, str]:
    """调用远端标准健康接口，返回是否 SERVING 及诊断文本。"""
    try:
        with open_channel_for(target, tls) as channel:
            stub: Any = health_pb2_grpc.HealthStub(channel)
            response = stub.Check(health_pb2.HealthCheckRequest(service=service), timeout=timeout)
    except grpc.RpcError as e:
        code = e.code() if hasattr(e, "code") else None
        details = e.details() if hasattr(e, "details") else str(e)
        return False, f"{code}: {details}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

    try:
        status_name = health_pb2.HealthCheckResponse.ServingStatus.Name(response.status)
    except ValueError:
        return False, f"UNKNOWN_STATUS({response.status})"
    return response.status == SERVING, status_name


__all__ = [
    "NOT_SERVING",
    "OVERALL_SERVICE",
    "SERVING",
    "TASK_SERVICE_NAME",
    "HealthReporter",
    "check_health",
]
