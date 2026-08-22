"""在异步入口中复用同步 gRPC channel 执行集群转发。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, final

import grpc
from grpc import ServicerContext
from typing_extensions import override

from ipclick.cluster.forwarder import (
    ForwardingTaskService,
    is_failover_safe,
    is_node_fault,
    propagate_rpc_error,
    rpc_detail,
    should_mark_unhealthy,
)
from ipclick.dto.proto import task_pb2
from ipclick.exceptions import TransportError
from ipclick.services.async_task_service import AsyncTaskService
from ipclick.services.task_service import is_forwarded
from ipclick.utils.config_util import section
from ipclick.utils.log_util import log


@final
class AsyncForwardingTaskService(AsyncTaskService, ForwardingTaskService):
    """将阻塞式下游转发移交线程池的异步转发服务。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # 转发用**专属**线程池，不能用 run_in_executor(None) 的默认池：那个池是整个
        # 事件循环共用的（默认只有 min(32, cpu+4) 个线程，4 核机器上就是 8 个），
        # 还同时承载着 SendStream 逐项推流和适配器的同步兜底。转发是阻塞调用、
        # 一占就是整个下游 RPC 的时长，挤在同一个池里能把本地执行和流式请求一起饿死。
        self._forward_pool: ThreadPoolExecutor = ThreadPoolExecutor(
            max_workers=max(2, self.settings_max_workers), thread_name_prefix="ipclick-forward"
        )

    @property
    def settings_max_workers(self) -> int:
        """转发线程池容量：跟随 [SERVER].max_workers。"""
        return int(section(self.config, "SERVER").get("max_workers") or 32)

    @override
    async def acleanup(self) -> None:
        """先停掉转发线程池，再走集群与适配器的收尾。"""
        self._forward_pool.shutdown(wait=False, cancel_futures=True)
        await super().acleanup()

    @override
    async def Send(self, request: "task_pb2.ReqTask", context: ServicerContext) -> "task_pb2.TaskResp":
        """选择节点执行请求，仅在节点级故障时尝试下一节点。"""
        if not self.cluster.forwarding_enabled or is_forwarded(context):
            self._local_count += 1
            return await AsyncTaskService.Send(self, request, context)

        # 与同步转发器同理：被转发的请求走不到 prepare()，入口节点的 SSRF 准入
        # 必须在这里显式施加一次，否则它对所有转发流量都不生效。
        rejected = self._reject_if_url_not_allowed(request, context)
        if rejected is not None:
            return rejected

        tried: set[str] = set()
        attempts = self.cluster.max_failover + 1
        last_error = ""
        loop = asyncio.get_running_loop()

        for attempt in range(attempts):
            try:
                state = self._pool.acquire(exclude=tried)
            except TransportError as e:
                last_error = str(e)
                break

            tried.add(state.node.id)
            if state.node.id == self.self_id:
                self._local_count += 1
                state.record_request(success=True)
                return await AsyncTaskService.Send(self, request, context)

            try:
                # 同步转发器里那条注释同样适用：转发流量原来不过入口的限流闸门，
                # 而开了转发就不做分片，N 个节点各放行完整配额。
                # 这里必须用 async_limiter——host_limiter 是另一个独立配额池，
                # 混用会让单 host 的有效并发翻倍（见 AsyncTaskService.SendStream 的说明）。
                async with self.async_limiter.acquire(request.url):
                    response = await loop.run_in_executor(self._forward_pool, self._forward, state, request)
            except ValueError as e:
                # channel 在停机或热更新的边缘被关掉时，grpc 抛的是 ValueError
                # （"Cannot invoke RPC on closed channel!"）而不是 RpcError，上面那个
                # except 接不住，异常会穿透 servicer：不记链路、不换节点、不本地兜底，
                # 调用方只看到一个 UNKNOWN。引用计数已经让这条路极难走到，这里是兜底。
                last_error = f"到节点 {state.node.id} 的连接已关闭：{e}"
                state.record_request(success=False)
                if not is_failover_safe(request):
                    log.warning(f"非幂等请求 {request.uuid} 遇到连接关闭，不重投：{last_error}")
                    return self._build_error_response(request, last_error)
                log.warning(f"转发到 {state.node.id} 时连接已关闭（第 {attempt + 1}/{attempts} 次），换一台重试")
                continue
            except grpc.RpcError as e:
                last_error = rpc_detail(e)
                state.record_request(success=False)
                if is_node_fault(e):
                    if should_mark_unhealthy(e):
                        state.mark_unhealthy(last_error)
                    if not is_failover_safe(request):
                        log.warning(
                            f"节点 {state.node.id} 处理非幂等请求 {request.uuid} 时结果未知，"
                            f"为避免重复执行，不再换节点或本地兜底：{last_error}"
                        )
                        propagate_rpc_error(context, e)
                        return self._build_error_response(request, last_error)
                    log.warning(f"转发到 {state.node.id} 失败（第 {attempt + 1}/{attempts} 次）：{last_error}")
                    continue
                # 参数、权限等业务错误在换节点后仍会失败，应原样返回给调用方。
                log.warning(f"节点 {state.node.id} 拒绝了请求 {request.uuid}：{last_error}")
                propagate_rpc_error(context, e)
                return self._build_error_response(request, last_error)
            state.record_request(success=True)
            self._forwarded_count += 1
            return response

        if self.cluster.node_by_id(self.self_id) is None:
            message = f"请求在 {len(tried)} 个节点上转发均失败，且入口节点未加入执行池：{last_error}"
            log.warning(f"请求 {request.uuid} {message}")
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            context.set_details(message)
            return self._build_error_response(request, message)

        if last_error:
            log.warning(f"所有节点均不可用，入口自己执行：{last_error}")
        self._local_count += 1
        return await AsyncTaskService.Send(self, request, context)

    @override
    def snapshot(self) -> dict[str, Any]:
        """返回与同步转发服务一致的集群运行快照。"""
        return ForwardingTaskService.snapshot(self)


__all__ = ["AsyncForwardingTaskService"]
