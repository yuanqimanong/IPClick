# -*- coding:utf-8 -*-

"""
@time: 2025-12-10
@author: Hades
@file: __init__.py
"""

import logging
import time
import traceback
from concurrent import futures

import grpc
import httpx

from ipclick.config_loader import load_config
from ipclick.dto.proto import task_pb2, task_pb2_grpc
from ipclick.utils.logger import LoggerFactory


class TaskService(task_pb2_grpc.TaskServiceServicer):
    """任务处理服务"""

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    def Send(self, request: task_pb2.ReqTask, context):
        """处理任务请求"""
        start_time = time.time()
        self.logger.info(f"Received request: {request.uuid} for URL: {request.url}")

        try:
            # 根据适配器选择处理方式
            if request.adapter == task_pb2.HTTPX:
                response = self._handle_httpx_request(request)
            else:
                # 默认使用httpx
                response = self._handle_httpx_request(request)

            elapsed_ms = int((time.time() - start_time) * 1000)
            response.response_time_ms = elapsed_ms

            self.logger.info(f"Request {request.uuid} completed in {elapsed_ms}ms, status: {response.status_code}")
            return response

        except Exception as e:
            elapsed_ms = int((time.time() - start_time) * 1000)
            self.logger.error(f"Request {request.uuid} failed: {str(e)}")
            self.logger.debug(traceback.format_exc())

            # 返回错误响应
            return task_pb2.TaskResp(
                request_uuid=request.uuid,
                adapter=request.adapter,
                original_request=request,
                effective_url=request.url,
                status_code=500,
                response_headers={},
                content=b'',
                error_message=str(e),
                response_time_ms=elapsed_ms
            )

    def _handle_httpx_request(self, request: task_pb2.ReqTask):
        """使用httpx处理HTTP请求"""
        # 构建请求参数
        method = self._convert_method(request.method)
        headers = dict(request.headers)
        params = dict(request.params)

        # 设置User-Agent
        if request.user_agent:
            headers['User-Agent'] = request.user_agent

        # 处理代理
        proxies = None
        if request.proxy and request.proxy.host:
            proxy_url = f"{request.proxy.scheme}://{request.proxy.host}:{request.proxy.port}"
            proxies = {"http://": proxy_url, "https://": proxy_url}

        # 处理请求体
        content = None
        json_data = None
        if request.text:
            try:
                # 尝试解析为JSON
                import json
                json_data = json.loads(request.text)
            except (json.JSONDecodeError, ValueError):
                # 如果不是JSON，作为普通文本处理
                content = request.text

        # 创建httpx客户端
        with httpx.Client(
                timeout=request.timeout_seconds,
                verify=request.verify_ssl,
                proxies=proxies,
                follow_redirects=True
        ) as client:

            # 执行请求，带重试逻辑
            last_exception = None
            for attempt in range(request.max_retries + 1):
                try:
                    if attempt > 0:
                        self.logger.info(f"Retrying request {request.uuid}, attempt {attempt + 1}")

                    response = client.request(
                        method=method,
                        url=request.url,
                        headers=headers,
                        params=params,
                        content=content,
                        json=json_data
                    )

                    # 构造响应
                    return task_pb2.TaskResp(
                        request_uuid=request.uuid,
                        adapter=request.adapter,
                        original_request=request,
                        effective_url=str(response.url),
                        status_code=response.status_code,
                        response_headers={k: v for k, v in response.headers.items()},
                        content=response.content,
                        error_message="",
                        response_time_ms=0  # 将在调用处设置
                    )

                except Exception as e:
                    last_exception = e
                    if attempt < request.max_retries:
                        time.sleep(min(2 ** attempt, 10))  # 指数退避
                    continue

            # 所有重试都失败了
            raise last_exception

    def _convert_method(self, pb_method):
        """转换protobuf方法为字符串"""
        method_map = {
            task_pb2.GET: "GET",
            task_pb2.POST: "POST",
            task_pb2.PUT: "PUT",
            task_pb2.DELETE: "DELETE",
            task_pb2.PATCH: "PATCH",
        }
        return method_map.get(pb_method, "GET")


def serve(config_path=None, port=None, host=None):
    """启动gRPC服务器"""
    # 加载配置
    config = load_config(config_path)

    # 命令行参数优先级最高
    server_port = port or config.get('SERVER', {}).get('port', 9527)
    server_host = host or config.get('SERVER', {}).get('host', '0.0.0.0')
    max_workers = config.get('SERVER', {}).get('max_workers', 10)

    # 配置日志
    LoggerFactory.setup_logging(config)
    logger = logging.getLogger(__name__)

    # 创建gRPC服务器
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    task_pb2_grpc.add_TaskServiceServicer_to_server(TaskService(), server)

    # 绑定地址
    listen_addr = f'{server_host}:{server_port}'
    server.add_insecure_port(listen_addr)

    # 启动服务器
    server.start()
    logger.info(f"IPClick server started on {listen_addr} with {max_workers} workers")

    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        logger.info("Shutting down server...")
        server.stop(grace=5)


if __name__ == '__main__':
    serve()
