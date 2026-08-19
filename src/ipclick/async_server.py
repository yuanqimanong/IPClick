"""``grpc.aio`` 服务端（0.7.0，实验性，默认关）。

由 ``[SERVER].async_mode`` 打开。与 :class:`~ipclick.server.IPClickServer` 并存，
**不是替换**——同步那条路仍然是默认，且短期内不会变。

为什么默认关（三条都是实打实会咬人的）：

1. **第三方适配器的线程安全假设变了。** README 声明支持注册自定义适配器，
   它们只实现同步 ``download()``。异步模式下这些实现被丢进 executor 跑：
   功能一致，但原来是"一请求一线程、串行进入我的对象"，现在是线程池复用线程。
   谁在适配器里用了 ``threading.local`` 做缓存，行为就变了，而且是静默的。
2. **Web 管理端「试一试」是同步调用。** 它直接调 ``task_service.Send()``，
   异步模式下需要跨线程投递到事件循环，那条路径的错误、取消、超时都得重写。
3. **0.6.0 刚换过一次并发模型（多进程）。** 一个版本里连换两次，出了问题
   没人分得清是哪一个引起的。

0.7 收集真实反馈，0.8 再考虑翻默认值。
"""

import asyncio
from concurrent import futures
from typing import Any

import grpc
from grpc import aio

from ipclick.auth import TokenAuthInterceptor
from ipclick.dto.proto import task_pb2_grpc
from ipclick.services.async_task_service import AsyncTaskService
from ipclick.utils.log_util import log


class _AsyncAuthInterceptor(aio.ServerInterceptor):
    """令牌鉴权的 aio 版。

    复用同步拦截器的判定逻辑（``TokenAuthInterceptor`` 里那套常量时间比较、
    免鉴权前缀、拒绝时记链路），只把外壳换成 aio 的接口——鉴权规则在两条路上
    必须完全一致，各写一份就等着哪天异步模式悄悄放行了本该拒绝的调用。
    """

    def __init__(self, delegate: TokenAuthInterceptor) -> None:
        self._delegate: TokenAuthInterceptor = delegate

    @property
    def enabled(self) -> bool:
        return self._delegate.enabled

    async def intercept_service(self, continuation: Any, handler_call_details: Any) -> Any:
        handler = self._delegate.intercept_service(lambda details: None, handler_call_details)
        # 同步拦截器放行时返回 continuation(...) 的结果（这里被我们喂了 None），
        # 拒绝时返回它自己构造的 _deny handler。所以"拿到了 handler"就等于被拒。
        if handler is not None:
            return aio.unary_unary_rpc_method_handler(_deny)
        return await continuation(handler_call_details)


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
    """按同步服务端的同一套参数造一个 aio server。

    ``max_workers`` 在这里的含义**变了**，值得说清楚：同步模式下它是"同时能处理
    多少请求"的硬上限（一请求一线程）；异步模式下协程不占线程，它只用于那个
    兜底的 executor——即适配器没实现异步、要把同步 ``download()`` 丢进去跑时。
    所以异步模式下把它调大不会提高并发上限，调小才会限制回退路径。
    """
    interceptors: list[aio.ServerInterceptor] = []
    if auth.enabled:
        interceptors.append(_AsyncAuthInterceptor(auth))

    return aio.server(
        # 只服务于"同步适配器的回退执行"，不再是并发上限。
        migration_thread_pool=futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="ipclick-fallback"
        ),
        maximum_concurrent_rpcs=max_concurrent_rpcs,
        interceptors=interceptors,
        options=[
            ("grpc.keepalive_time_ms", 60000),
            ("grpc.keepalive_timeout_ms", 30000),
            ("grpc.keepalive_permit_without_calls", True),
            ("grpc.http2.max_pings_without_data", 2),
            ("grpc.http2.min_time_between_pings_ms", 10000),
            ("grpc.http2.min_ping_interval_without_data_ms", 120000),
            ("grpc.max_send_message_length", 500 * 1024 * 1024),
            ("grpc.max_receive_message_length", 500 * 1024 * 1024),
            ("grpc.max_concurrent_streams", max_concurrent_streams),
            ("grpc.enable_http_proxy", 0),
            ("grpc.so_reuseport", 1 if reuseport else 0),
        ],
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
    """起一个 aio 服务端并等它终止。"""
    server = build_async_server(**server_kwargs)
    task_pb2_grpc.add_TaskServiceServicer_to_server(service, server)

    if health_enabled:
        # 必须用 aio 版：同步 HealthServicer 的 set() 不是协程，await 它会抛
        # TypeError 把整个服务端带崩，而症状是"客户端连不上"——很容易被误读
        # 成端口或防火墙问题。
        from grpc_health.v1 import health_pb2, health_pb2_grpc
        from grpc_health.v1.health import aio as health_aio

        servicer = health_aio.HealthServicer()
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


__all__ = ["AsyncTaskService", "build_async_server", "serve_async"]
