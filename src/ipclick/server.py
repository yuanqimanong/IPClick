import logging
import time
from concurrent import futures

import grpc

from ipclick.dto.proto import task_pb2, task_pb2_grpc


class TaskService(task_pb2_grpc.TaskServiceServicer):
    def Send(self, request: task_pb2.ReqTask, context):
        # 模拟处理请求
        print(f"Received request: {request.uuid} for URL: {request.url}")

        # 模拟响应时间
        time.sleep(0.1)

        # 构造响应
        response = task_pb2.TaskResp(
            request_uuid=request.uuid,
            adapter=request.adapter,
            original_request=request,
            effective_url=request.url,
            status_code=200,
            response_headers={"Content-Type": "application/json"},
            content=b'{"message": "success"}',
            error_message="",
            response_time_ms=100
        )

        return response


def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    task_pb2_grpc.add_TaskServiceServicer_to_server(TaskService(), server)
    server.add_insecure_port('[::]:50051')
    server.start()
    print("Task service server started on port 50051...")
    server.wait_for_termination()


if __name__ == '__main__':
    logging.basicConfig()
    serve()
