"""异步模式下的服务端转发（0.7.0）。

解掉 ``[SERVER].async_mode`` 与 ``[CLUSTER].forward = "on"`` 的互斥。

设计上是**组合而非重写**：路由决策（挑节点、故障转移、防环路、健康计数）
全部复用 :class:`~ipclick.cluster.forwarder.ForwardingTaskService`，本文件
只决定"这一跳怎么执行"——

* 本地执行 → 走 :class:`~ipclick.services.async_task_service.AsyncTaskService`
  那条真异步路径，拿到协程的全部好处
* 转发给别的节点 → 出站那一跳仍是**同步 gRPC stub**，丢进线程池执行

为什么出站不一并换成 ``grpc.aio``：那需要重做连接池、TLS 凭据、故障转移与
健康计数这一整套，而它们现在是转发模式最要紧、也最经得起考验的部分。
把它们照搬到 aio 上是一次独立的重构，风险不该和"服务端换并发模型"捆在一起。

**代价说清楚**：被转发出去的请求，每个仍占一个线程直到对端回话。所以异步模式
对纯转发流量的收益有限；收益集中在**入口自己执行**的那部分（``forward="on"``
时入口本来就在 nodes 里，会分到相当比例的活）。想让转发那一跳也不占线程，
等后续把出站换成 aio。
"""

import asyncio
from typing import Any

from grpc import ServicerContext
from typing_extensions import override

from ipclick.cluster.forwarder import ForwardingTaskService, is_forwarded
from ipclick.dto.proto import task_pb2
from ipclick.exceptions import TransportError
from ipclick.services.async_task_service import AsyncTaskService
from ipclick.utils.log_util import log


class AsyncForwardingTaskService(AsyncTaskService, ForwardingTaskService):
    """异步 + 服务端转发。

    MRO 是 AsyncTaskService → ForwardingTaskService → TaskService：
    构造走 ForwardingTaskService（节点池、self_id、TLS 那一套），
    RPC 入口走 AsyncTaskService，而 :meth:`Send` 在这里显式编排两者。
    """

    @override
    async def Send(self, request: "task_pb2.ReqTask", context: ServicerContext) -> "task_pb2.TaskResp":
        if is_forwarded(context):
            # 已经是转发来的：绝不再转。这是防环路的唯一依据，别加任何例外。
            self._local_count += 1
            return await AsyncTaskService.Send(self, request, context)

        tried: set[str] = set()
        attempts = self.cluster.max_failover + 1
        last_error = ""
        loop = asyncio.get_running_loop()

        for _ in range(attempts):
            try:
                state = self._pool.acquire(exclude=tried)
            except TransportError as e:
                # 没有别的节点可试了——入口自己兜底，别让请求凭空失败。
                last_error = str(e)
                break

            tried.add(state.node.id)
            if state.node.id == self.self_id:
                self._local_count += 1
                state.record_request(success=True)
                return await AsyncTaskService.Send(self, request, context)

            try:
                # 出站这一跳仍是同步 stub，丢进线程池：不阻塞事件循环，
                # 但确实占一个线程直到对端回话（见模块注释里的取舍说明）。
                response = await loop.run_in_executor(None, self._forward, state, request)
            except Exception as e:
                last_error = str(e)
                log.warning(f"转发到节点 {state.node.id} 失败，尝试下一个：{e}")
                continue
            return response

        if last_error:
            log.warning(f"所有节点均不可用，入口自己执行：{last_error}")
        self._local_count += 1
        return await AsyncTaskService.Send(self, request, context)

    @override
    def snapshot(self) -> dict[str, Any]:
        return ForwardingTaskService.snapshot(self)


__all__ = ["AsyncForwardingTaskService"]
