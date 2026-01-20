# -*- coding:utf-8 -*-

"""
任务处理服务

@time: 2025-12-10
@author: Hades
@file: task_service.py
"""

import json
import time
from typing import Any

from ipclick.adapters import create_adapter, get_adapter_info, get_default_adapter
from ipclick.dto import Response
from ipclick.dto.models import ADAPTER_MAP, METHOD_MAP
from ipclick.dto.proto import task_pb2, task_pb2_grpc
from ipclick.utils import json_hook
from ipclick.utils.config_util import Settings
from ipclick.utils.log_util import log


class TaskService(task_pb2_grpc.TaskServiceServicer):
    """
    重构后的任务处理服务

    职责：
    1. 处理gRPC请求
    2. 选择合适的HTTP适配器
    3. 执行下载任务
    4. 转换响应格式
    5. 错误处理和日志记录
    """

    def __init__(self, config: Settings):
        self.config: Settings = config

        # 适配器缓存
        self._adapters = {}

        # 获取默认适配器
        try:
            self.default_adapter = get_default_adapter()
        except RuntimeError as e:
            log.error(f"No adapters available: {e}")
            raise

        # 适配器配置
        self.adapter_configs = {
            "DOWNLOADER": self.config.get("DOWNLOADER", {}),
            "BROWSER": self.config.get("BROWSER", {}),
        }

        # 记录初始化信息
        log.info(f"TaskService initialized with default adapter: {self.default_adapter}")
        self._log_adapter_info()

    def _log_adapter_info(self):
        """记录适配器信息"""
        try:
            adapter_info = get_adapter_info()
            log.info(f"Available adapters: {list(adapter_info.keys())}")
            for name, info in adapter_info.items():
                log.debug(f"  {name}: {info['class']} ({info['module']})")
        except Exception as e:
            log.warning(f"Could not get adapter info: {e}")

    def Send(self, request: task_pb2.ReqTask, context) -> task_pb2.TaskResp:
        """
        处理gRPC任务请求

        Args:
            request: gRPC请求对象
            context: gRPC上下文

        Returns:
            task_pb2.TaskResp: gRPC响应对象
        """
        log.info(f"Received request: {request.uuid} for URL: {request.url}")
        start_time = time.time()

        # 选择适配器
        adapter_name = self._get_adapter_name(request.adapter)
        adapter = self._get_adapter(adapter_name)

        # 执行下载
        response = self._execute_download(adapter, request)

        # 构造gRPC响应
        grpc_response = self._build_grpc_response(request, response)

        # 设置响应时间
        elapsed_ms = int((time.time() - start_time) * 1000)
        grpc_response.response_time_ms = elapsed_ms

        # 记录成功日志
        log.info(
            f"Request {request.uuid} completed in {elapsed_ms}ms, "
            f"status:  {grpc_response.status_code}, adapter: {adapter_name}"
        )

        return grpc_response

    def _get_adapter_name(self, pb_adapter: int) -> str:
        """
        根据protobuf适配器枚举获取适配器名称

        Args:
            pb_adapter: protobuf适配器枚举值

        Returns:
            str: 适配器名称
        """

        # 如果没有指定或者是未知枚举，使用默认适配器
        if pb_adapter not in ADAPTER_MAP:
            log.debug(f"Unknown adapter enum {pb_adapter}, using default:  {self.default_adapter}")
            return self.default_adapter

        return ADAPTER_MAP[pb_adapter]

    def _get_adapter(self, adapter_name: str):
        """
        获取适配器实例（带缓存）

        Args:
            adapter_name:  适配器名称

        Returns:
            Downloader: 适配器实例
        """
        if adapter_name not in self._adapters:
            # 创建适配器实例
            adapter = create_adapter(adapter_name)

            # TODO 应用配置
            # self._configure_adapter(adapter)

            # 缓存适配器
            self._adapters[adapter_name] = adapter
            log.debug(f"Created adapter instance: {adapter_name}")

        return self._adapters[adapter_name]

    def _configure_adapter(self, adapter):
        """
        配置适配器实例

        Args:
            adapter: 适配器实例
            config: 配置字典
        """
        # 通用配置项
        if "max_retries" in self.adapter_configs:
            adapter.max_retries = self.adapter_configs["max_retries"]
        if "retry_delay" in self.adapter_configs:
            adapter.retry_delay = self.adapter_configs["retry_delay"]
        if "timeout" in self.adapter_configs:
            adapter.timeout = self.adapter_configs["timeout"]
        if "verify_ssl" in self.adapter_configs:
            adapter.verify_ssl = self.adapter_configs["verify_ssl"]

        # curl_cffi特定配置
        if hasattr(adapter, "impersonate") and "impersonate" in self.adapter_configs:
            adapter.impersonate = self.adapter_configs["impersonate"]

    def _execute_download(self, adapter, request: task_pb2.ReqTask) -> Response:
        """
        执行下载请求

        Args:
            adapter: HTTP适配器
            request: gRPC请求对象

        Returns:
            Response: 统一响应对象
        """
        # 转换HTTP方法
        method = METHOD_MAP.get(request.method, "GET")

        # 处理请求头
        headers = dict(request.headers) if request.headers else None
        cookies = dict(request.cookies) if request.cookies else None
        params = json.loads(request.params, object_hook=json_hook) if request.params else None
        # 处理请求体
        data = json.loads(request.data) if request.data else None
        json_data = json.loads(request.json) if request.json else None

        # 构建下载参数
        download_kwargs = {
            "method": method,
            "headers": headers,
            "cookies": cookies,
            "params": params,
            "data": data,
            "json": json_data,
            "proxy": request.proxy,
            "timeout": request.timeout_seconds,
            "max_retries": request.max_retries,
            "retry_delay": request.retry_backoff_seconds,
            "verify": request.verify_ssl,
            "allow_redirects": request.allow_redirects,
            "stream": request.stream,
            "impersonate": request.impersonate,
            "extensions": request.extensions,
            "automation_config": request.automation_config,
            "automation_script": request.automation_script,
            "kwargs": request.kwargs,
        }

        # 执行下载
        return adapter.download(request.url, **download_kwargs)

    def _build_grpc_response(self, request: task_pb2.ReqTask, response: Response) -> task_pb2.TaskResp:
        """
        构建gRPC响应

        Args:
            request: 原始gRPC请求
            response:  统一响应对象

        Returns:
            task_pb2.TaskResp: gRPC响应对象
        """
        return task_pb2.TaskResp(
            request_uuid=request.uuid,
            adapter=request.adapter,
            original_request=request,
            effective_url=response.url,
            status_code=response.status_code,
            response_headers=response.headers or {},
            content=response.content or b"",
            error_message=str(response.exception) if response.exception else "",
            response_time_ms=response.elapsed_ms,
        )

    def cleanup(self):
        """
        清理资源

        关闭所有适配器连接，释放资源
        """
        log.info("Cleaning up TaskService resources...")

        for name, adapter in self._adapters.items():
            try:
                adapter.close()
                log.debug(f"Closed adapter: {name}")
            except Exception as e:
                log.warning(f"Error closing adapter {name}: {e}")

        self._adapters.clear()
        log.info("TaskService cleanup completed")

    def get_stats(self) -> dict[str, Any]:
        """
        获取服务统计信息

        Returns:
            Dict:  统计信息
        """
        return {
            "default_adapter": self.default_adapter,
            "active_adapters": list(self._adapters.keys()),
            "adapter_count": len(self._adapters),
            "config": {"adapters": self.adapter_configs, "default": self.default_adapter},
        }
