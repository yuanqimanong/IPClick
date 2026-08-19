from __future__ import annotations

from concurrent import futures
from typing import Any

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from ipclick.tls import TLSSettings, channel_credentials, channel_options
from ipclick.utils.log_util import log


OVERALL_SERVICE = ""

TASK_SERVICE_NAME = "task.TaskService"

SERVING = health_pb2.HealthCheckResponse.SERVING
NOT_SERVING = health_pb2.HealthCheckResponse.NOT_SERVING


class HealthReporter:
    def __init__(self, enabled: bool = True, max_workers: int = 2):
        self.enabled: bool = enabled
        self._servicer: health.HealthServicer | None = (
            health.HealthServicer(experimental_thread_pool=futures.ThreadPoolExecutor(max_workers=max_workers))
            if enabled
            else None
        )

    @property
    def servicer(self) -> health.HealthServicer | None:
        return self._servicer

    def register(self, server: grpc.Server) -> None:
        if self._servicer is None:
            log.info("健康检查未启用（[MONITOR].health_check = false）")
            return
        health_pb2_grpc.add_HealthServicer_to_server(self._servicer, server)
        log.debug("已注册 grpc.health.v1 健康检查服务")

    def set_serving(self) -> None:
        self._set(SERVING)

    def set_not_serving(self) -> None:
        self._set(NOT_SERVING)

    def _set(self, status: int) -> None:
        if self._servicer is None:
            return
        for service in (OVERALL_SERVICE, TASK_SERVICE_NAME):
            self._servicer.set(service, status)
        name = health_pb2.HealthCheckResponse.ServingStatus.Name(status)
        log.debug(f"健康状态 -> {name}")

    def enter_graceful_shutdown(self) -> None:
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
    try:
        settings = tls or TLSSettings()
        options: list[tuple[str, Any]] = [("grpc.enable_http_proxy", 0), *channel_options(settings)]
        channel_ctx = (
            grpc.secure_channel(target, channel_credentials(settings), options=options)
            if settings.enabled
            else grpc.insecure_channel(target, options=options)
        )
        with channel_ctx as channel:
            stub: Any = health_pb2_grpc.HealthStub(channel)
            response = stub.Check(health_pb2.HealthCheckRequest(service=service), timeout=timeout)
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
