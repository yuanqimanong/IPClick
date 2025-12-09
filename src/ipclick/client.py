import uuid

import grpc

from ipclick.dto.proto import task_pb2, task_pb2_grpc


def run():
    # 连接服务器
    with grpc.insecure_channel('localhost:50051') as channel:
        stub = task_pb2_grpc.TaskServiceStub(channel)

        # 创建请求任务
        request = task_pb2.ReqTask(
            uuid=str(uuid.uuid4()),
            adapter=task_pb2.HTTPX,
            url="https://example.com/api",
            method=task_pb2.POST,
            headers={"Authorization": "Bearer token"},
            params={"query": "test"},
            text='{"data": "test"}',
            timeout_seconds=30,
            proxy=task_pb2.ProxyInfo(
                scheme="http",
                host="proxy.example.com",
                port=8080
            ),
            max_retries=3,
            verify_ssl=True,
            user_agent="IPClick Client v1.0"
        )

        # 发送请求并获取响应
        try:
            response = stub.Send(request)
            print(f"Response UUID: {response.request_uuid}")
            print(f"Status Code: {response.status_code}")
            print(f"Effective URL: {response.effective_url}")
            print(f"Response Time: {response.response_time_ms} ms")
            print(f"Content: {response.content.decode()}")
        except grpc.RpcError as e:
            print(f"gRPC error: {e}")


if __name__ == '__main__':
    for i in range(100):
        run()
