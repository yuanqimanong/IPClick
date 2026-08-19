import asyncio
from typing import Any, final

from grpc import ServicerContext
from typing_extensions import override

from ipclick.cluster.forwarder import ForwardingTaskService
from ipclick.dto.proto import task_pb2
from ipclick.exceptions import TransportError
from ipclick.services.async_task_service import AsyncTaskService
from ipclick.services.task_service import is_forwarded
from ipclick.utils.log_util import log


@final
class AsyncForwardingTaskService(AsyncTaskService, ForwardingTaskService):
    @override
    async def Send(self, request: "task_pb2.ReqTask", context: ServicerContext) -> "task_pb2.TaskResp":
        if is_forwarded(context):
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
                last_error = str(e)
                break

            tried.add(state.node.id)
            if state.node.id == self.self_id:
                self._local_count += 1
                state.record_request(success=True)
                return await AsyncTaskService.Send(self, request, context)

            try:
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
