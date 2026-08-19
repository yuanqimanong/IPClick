import asyncio
from concurrent import futures
from typing import Any

import grpc
from grpc import aio
from typing_extensions import override

from ipclick.auth import TokenAuthInterceptor, extract_token, is_exempt, token_matches
from ipclick.dto.proto import task_pb2_grpc
from ipclick.rpc import server_options
from ipclick.services.async_task_service import AsyncTaskService
from ipclick.trace import get_recorder
from ipclick.utils.log_util import log


class _AsyncAuthInterceptor(aio.ServerInterceptor):
    def __init__(self, delegate: TokenAuthInterceptor) -> None:
        self._delegate: TokenAuthInterceptor = delegate

    @property
    def enabled(self) -> bool:
        return self._delegate.enabled

    @override
    async def intercept_service(self, continuation: Any, handler_call_details: Any) -> Any:
        if not self._delegate.enabled:
            return await continuation(handler_call_details)

        method: str = getattr(handler_call_details, "method", "") or ""
        if is_exempt(method):
            return await continuation(handler_call_details)

        token = extract_token(getattr(handler_call_details, "invocation_metadata", None))
        if token_matches(token, self._delegate.tokens):
            return await continuation(handler_call_details)

        log.warning(f"拒绝未通过鉴权的调用: {method}")
        get_recorder().record_rejected("unauthenticated")
        return grpc.unary_unary_rpc_method_handler(_deny)


async def _deny(_request: Any, context: Any) -> Any:
    await context.abort(grpc.StatusCode.UNAUTHENTICATED, "缺少或无效的鉴权令牌")


def build_async_server(
    *,
    max_workers: int,
    max_concurrent_rpcs: int,
    max_concurrent_streams: int,
    compression: grpc.Compression,
    auth: TokenAuthInterceptor,
    reuseport: bool,
) -> aio.Server:
    interceptors: list[aio.ServerInterceptor] = []
    if auth.enabled:
        interceptors.append(_AsyncAuthInterceptor(auth))

    return aio.server(
        migration_thread_pool=futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="ipclick-fallback"
        ),
        maximum_concurrent_rpcs=max_concurrent_rpcs,
        interceptors=interceptors,
        options=server_options(max_concurrent_streams=max_concurrent_streams, reuseport=reuseport),
        compression=compression,
    )


async def serve_async(
    service: AsyncTaskService,
    listen_addr: str,
    *,
    credentials: grpc.ServerCredentials | None = None,
    health_enabled: bool = True,
    **server_kwargs: Any,
) -> None:
    service.bind_loop(asyncio.get_running_loop())

    server = build_async_server(**server_kwargs)
    task_pb2_grpc.add_TaskServiceServicer_to_server(service, server)

    if health_enabled:
        from grpc_health.v1 import health, health_pb2, health_pb2_grpc

        health_dynamic: Any = health
        servicer = health_dynamic.aio.HealthServicer()
        health_pb2_grpc.add_HealthServicer_to_server(servicer, server)
        await servicer.set("", health_pb2.HealthCheckResponse.SERVING)

    bound = (
        server.add_secure_port(listen_addr, credentials)
        if credentials is not None
        else server.add_insecure_port(listen_addr)
    )
    if bound == 0:
        raise RuntimeError(f"Failed to bind to address {listen_addr}")

    await server.start()
    log.info(f"IPClick async server started on {listen_addr}（实验性：[SERVER].async_mode）")
    try:
        await server.wait_for_termination()
    except asyncio.CancelledError:
        await server.stop(grace=10)
        raise
    finally:
        await service.acleanup()


__all__ = ["AsyncTaskService", "build_async_server", "serve_async"]
