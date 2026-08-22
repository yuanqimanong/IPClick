"""基于 ``grpc.aio`` 的实验性异步服务端启动与停机逻辑。"""

import asyncio
from concurrent import futures
import contextlib
import signal
from typing import Any

import grpc
from grpc import aio
from typing_extensions import override

from ipclick.auth import TokenAuthInterceptor, extract_token, is_exempt, token_matches
from ipclick.dto.proto import task_pb2_grpc
from ipclick.rpc import server_options
from ipclick.server_settings import GRACE_PERIOD_SECONDS
from ipclick.services.async_task_service import AsyncTaskService
from ipclick.trace import get_recorder
from ipclick.utils.log_util import log


class _AsyncAuthInterceptor(aio.ServerInterceptor):
    """为异步 gRPC server 复用同步鉴权器中的令牌配置。"""

    def __init__(self, delegate: TokenAuthInterceptor) -> None:
        self._delegate: TokenAuthInterceptor = delegate

    @override
    async def intercept_service(self, continuation: Any, handler_call_details: Any) -> Any:
        """校验 metadata，并保留被拦截 RPC 原有的流式形态。"""
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
        handler = await continuation(handler_call_details)
        if handler is None:
            return grpc.unary_unary_rpc_method_handler(_deny)
        kwargs = {
            "request_deserializer": handler.request_deserializer,
            "response_serializer": handler.response_serializer,
        }
        if handler.request_streaming:
            if handler.response_streaming:
                return grpc.stream_stream_rpc_method_handler(_deny_stream, **kwargs)
            return grpc.stream_unary_rpc_method_handler(_deny, **kwargs)
        if handler.response_streaming:
            return grpc.unary_stream_rpc_method_handler(_deny_stream, **kwargs)
        return grpc.unary_unary_rpc_method_handler(_deny, **kwargs)


async def _deny(_request: Any, context: Any) -> Any:
    await context.abort(grpc.StatusCode.UNAUTHENTICATED, "缺少或无效的鉴权令牌")


async def _deny_stream(_request: Any, context: Any) -> Any:
    await context.abort(grpc.StatusCode.UNAUTHENTICATED, "缺少或无效的鉴权令牌")
    yield  # pragma: no cover - context.abort 始终抛出异常


def build_async_server(
    *,
    max_workers: int,
    max_concurrent_rpcs: int,
    max_concurrent_streams: int,
    compression: grpc.Compression,
    auth: TokenAuthInterceptor,
    reuseport: bool,
) -> aio.Server:
    """按并发、压缩、鉴权和端口复用设置创建异步 server。"""
    # 启动时无令牌也挂载拦截器，以便集群热更新后立即启用鉴权。
    interceptors: list[aio.ServerInterceptor] = [_AsyncAuthInterceptor(auth)]

    return aio.server(
        migration_thread_pool=futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="ipclick-fallback"
        ),
        maximum_concurrent_rpcs=max_concurrent_rpcs,
        interceptors=interceptors,
        options=server_options(max_concurrent_streams=max_concurrent_streams, reuseport=reuseport),
        compression=compression,
    )


async def _serve_until_signalled(server: Any) -> "asyncio.Future[Any]":
    """等待服务终止，或等到收到停机信号。

    同步服务端在 IPClickServer 里装了 signal.signal，异步这条路上一个都没有——
    默认处置就是**直接杀进程**：在途 RPC 在响应中途被切断（客户端收到 UNAVAILABLE
    而不是正常完成），service.acleanup() 不跑（浏览器适配器起的进程留成孤儿），
    链路记录缓冲里没落盘的那部分直接丢。容器里 docker stop / kubectl delete pod
    发的就是 SIGTERM，所以这不是边角情况。

    用 loop.add_signal_handler 而不是 signal.signal：后者在协程里触发时无法唤醒
    正在 await 的事件循环，只能等下一次 IO 才被处理。
    """
    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    installed: list[Any] = []

    for name in ("SIGINT", "SIGTERM"):
        received = getattr(signal, name, None)
        if received is None:
            continue
        try:
            loop.add_signal_handler(received, stopping.set)
        except (NotImplementedError, RuntimeError):
            # Windows 的事件循环不支持 add_signal_handler；那边本来也不走多进程。
            log.debug(f"当前事件循环不支持接管 {name}，停机将依赖上层处理")
            continue
        installed.append(received)

    termination = asyncio.ensure_future(server.wait_for_termination())
    signalled = asyncio.ensure_future(stopping.wait())
    try:
        done, _pending = await asyncio.wait({termination, signalled}, return_when=asyncio.FIRST_COMPLETED)
        if signalled in done:
            log.info("收到停机信号，开始优雅停机…")
    finally:
        if not signalled.done():
            _ = signalled.cancel()
        # 刻意**不**取消 termination：grpc.aio 的 wait_for_termination() 绑着 server 自己的
        # 停机状态机，取消它会把 server 一起打进 cancelled——随后 server.stop(grace=10) 立刻
        # 抛 CancelledError，于是 grace 期完全不生效（在途请求被切断成 status_code=0、
        # error=None 的"假响应"，调用方按 if resp.error 判断根本发现不了），异常还会一路穿到
        # CLI 顶层让进程以退出码 1 结束。留着它，由下面的 server.stop() 让它自然完成。
        for received in installed:
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(received)
    return termination


async def serve_async(
    service: AsyncTaskService,
    listen_addr: str,
    *,
    credentials: grpc.ServerCredentials | None = None,
    health_enabled: bool = True,
    grace: float = GRACE_PERIOD_SECONDS,
    **server_kwargs: Any,
) -> None:
    """启动异步 gRPC 服务，并在取消或终止时释放 service 资源。"""
    service.bind_loop(asyncio.get_running_loop())

    server = build_async_server(**server_kwargs)
    task_pb2_grpc.add_TaskServiceServicer_to_server(service, server)

    health_servicer: Any = None
    serving_status: int | None = None
    if health_enabled:
        from grpc_health.v1 import health, health_pb2, health_pb2_grpc

        health_dynamic: Any = health
        health_servicer = health_dynamic.aio.HealthServicer()
        serving_status = health_pb2.HealthCheckResponse.SERVING
        health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)

    started = False
    termination: asyncio.Future[Any] | None = None
    try:
        bound = (
            server.add_secure_port(listen_addr, credentials)
            if credentials is not None
            else server.add_insecure_port(listen_addr)
        )
        if bound == 0:
            raise RuntimeError(f"Failed to bind to address {listen_addr}")

        await server.start()
        started = True
        if health_servicer is not None and serving_status is not None:
            # 与同步服务一致，同时公布整体和 TaskService 的健康状态。
            await health_servicer.set("", serving_status)
            await health_servicer.set("task.TaskService", serving_status)
        log.info(f"IPClick async server started on {listen_addr}（实验性：[SERVER].async_mode）")
        termination = await _serve_until_signalled(server)
    except asyncio.CancelledError:
        raise
    finally:
        try:
            if health_servicer is not None:
                await health_servicer.enter_graceful_shutdown()
            # 未成功 start 的 server 也持有 migration thread pool，仍需显式 stop。
            # grace 原来写死成 10：调用方（含 [SERVER] 里配出来的停机预算）传什么都没用。
            await server.stop(grace=grace if started else 0)
            if termination is not None and not termination.done():
                # stop() 之后它必然很快完成；显式等一下，免得留下 pending task 警告。
                with contextlib.suppress(Exception):
                    await termination
        finally:
            await service.acleanup()


__all__ = ["AsyncTaskService", "build_async_server", "serve_async"]
