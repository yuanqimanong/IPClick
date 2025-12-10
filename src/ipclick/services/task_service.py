# -*- coding:utf-8 -*-

"""
任务处理服务

@time: 2025-12-10
@author: Hades
@file: task_service.py
"""
import logging
import time
import traceback
from typing import Optional, Dict, Any

from ipclick.adapters import get_default_adapter, get_adapter_info, create_adapter
from ipclick.dto import Response
from ipclick.dto.proto import task_pb2_grpc, task_pb2


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

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.logger = logging.getLogger(__name__)

        # 适配器缓存
        self._adapters = {}

        # 获取默认适配器
        try:
            self.default_adapter = self.config.get('adapters', {}).get('default') or get_default_adapter()
        except RuntimeError as e:
            self.logger.error(f"No adapters available: {e}")
            raise

        # 适配器配置
        self.adapter_configs = self.config.get('adapters', {})

        # 记录初始化信息
        self.logger.info(f"TaskService initialized with default adapter: {self.default_adapter}")
        self._log_adapter_info()

    def _log_adapter_info(self):
        """记录适配器信息"""
        try:
            adapter_info = get_adapter_info()
            self.logger.info(f"Available adapters: {list(adapter_info.keys())}")
            for name, info in adapter_info.items():
                self.logger.debug(f"  {name}: {info['class']} ({info['module']})")
        except Exception as e:
            self.logger.warning(f"Could not get adapter info: {e}")

    def Send(self, request: task_pb2.ReqTask, context) -> task_pb2.TaskResp:
        """
        处理gRPC任务请求

        Args:
            request: gRPC请求对象
            context: gRPC上下文

        Returns:
            task_pb2.TaskResp: gRPC响应对象
        """
        start_time = time.time()
        self.logger.info(f"Received request: {request.uuid} for URL: {request.url}")

        try:
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
            self.logger.info(
                f"Request {request.uuid} completed in {elapsed_ms}ms, "
                f"status:  {grpc_response.status_code}, adapter: {adapter_name}"
            )

            return grpc_response

        except Exception as e:
            elapsed_ms = int((time.time() - start_time) * 1000)
            self.logger.error(f"Request {request.uuid} failed: {str(e)}")
            self.logger.debug(traceback.format_exc())

            # 返回错误响应
            return self._build_error_response(request, e, elapsed_ms)

    def _get_adapter_name(self, pb_adapter: int) -> str:
        """
        根据protobuf适配器枚举获取适配器名称

        Args:
            pb_adapter: protobuf适配器枚举值

        Returns:
            str: 适配器名称
        """
        # protobuf适配器映射
        adapter_mapping = {
            task_pb2.HTTPX: 'httpx',
            task_pb2.CURL_CFFI: 'curl_cffi',
        }

        # 如果有新的适配器枚举，可以在这里添加
        if hasattr(task_pb2, 'REQUESTS'):
            adapter_mapping[task_pb2.REQUESTS] = 'requests'
        if hasattr(task_pb2, 'AIOHTTP'):
            adapter_mapping[task_pb2.AIOHTTP] = 'aiohttp'

        # 如果没有指定或者是未知枚举，使用默认适配器
        if pb_adapter not in adapter_mapping:
            self.logger.debug(f"Unknown adapter enum {pb_adapter}, using default:  {self.default_adapter}")
            return self.default_adapter

        adapter_name = adapter_mapping[pb_adapter]

        # 检查指定的适配器是否可用
        try:
            from ..adapters import get_adapter
            get_adapter(adapter_name)
            return adapter_name
        except ValueError:
            self.logger.warning(
                f"Adapter {adapter_name} not available, using default: {self.default_adapter}"
            )
            return self.default_adapter

    def _get_adapter(self, adapter_name: str):
        """
        获取适配器实例（带缓存）

        Args:
            adapter_name:  适配器名称

        Returns:
            Downloader: 适配器实例
        """
        if adapter_name not in self._adapters:
            # 获取适配器特定配置
            adapter_config = self.adapter_configs.get(adapter_name, {})

            # 创建适配器实例
            adapter = create_adapter(adapter_name)

            # 应用配置
            self._configure_adapter(adapter, adapter_config)

            # 缓存适配器
            self._adapters[adapter_name] = adapter

            self.logger.debug(f"Created adapter instance: {adapter_name}")

        return self._adapters[adapter_name]

    def _configure_adapter(self, adapter, config: Dict[str, Any]):
        """
        配置适配器实例

        Args:
            adapter: 适配器实例
            config: 配置字典
        """
        # 通用配置项
        if 'max_retries' in config:
            adapter.max_retries = config['max_retries']
        if 'retry_delay' in config:
            adapter.retry_delay = config['retry_delay']
        if 'timeout' in config:
            adapter.timeout = config['timeout']
        if 'verify_ssl' in config:
            adapter.verify_ssl = config['verify_ssl']

        # curl_cffi特定配置
        if hasattr(adapter, 'impersonate') and 'impersonate' in config:
            adapter.impersonate = config['impersonate']

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
        method = self._convert_method(request.method)

        # 构建请求参数
        headers = dict(request.headers) if request.headers else {}
        params = dict(request.params) if request.params else {}

        # 设置User-Agent
        if request.user_agent:
            headers['User-Agent'] = request.user_agent

        # 处理代理
        proxy = None
        if request.proxy and request.proxy.host:
            scheme = request.proxy.scheme or 'http'
            host = request.proxy.host
            port = request.proxy.port or 8080
            proxy = f"{scheme}://{host}:{port}"

        # 处理请求体
        data = None
        json_data = None
        if request.text:
            try:
                import json
                json_data = json.loads(request.text)
            except (json.JSONDecodeError, ValueError):
                # 如果不是JSON，作为普通文本处理
                data = request.text

        # 构建下载参数
        download_kwargs = {
            'method': method,
            'headers': headers,
            'params': params,
            'timeout': request.timeout_seconds if request.timeout_seconds > 0 else None,
            'proxy': proxy,
            'max_retries': request.max_retries,
        }

        # 添加SSL验证
        if hasattr(request, 'verify_ssl'):
            download_kwargs['verify'] = request.verify_ssl

        # 添加请求体
        if json_data is not None:
            download_kwargs['json'] = json_data
        elif data is not None:
            download_kwargs['data'] = data

        # 执行下载
        return adapter.download(request.url, **download_kwargs)

    def _convert_method(self, pb_method: int) -> str:
        """
        转换protobuf方法枚举为字符串

        Args:
            pb_method: protobuf方法枚举

        Returns:
            str: HTTP方法字符串
        """
        method_mapping = {
            task_pb2.GET: "GET",
            task_pb2.POST: "POST",
            task_pb2.PUT: "PUT",
            task_pb2.DELETE: "DELETE",
            task_pb2.PATCH: "PATCH",
        }

        # 添加可能存在的方法
        if hasattr(task_pb2, 'HEAD'):
            method_mapping[task_pb2.HEAD] = "HEAD"
        if hasattr(task_pb2, 'OPTIONS'):
            method_mapping[task_pb2.OPTIONS] = "OPTIONS"

        return method_mapping.get(pb_method, "GET")

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
            content=response.content or b'',
            error_message=str(response.exception) if response.exception else "",
            response_time_ms=response.elapsed_ms
        )

    def _build_error_response(self, request: task_pb2.ReqTask,
                              error: Exception, elapsed_ms: int) -> task_pb2.TaskResp:
        """
        构建错误响应

        Args:
            request: 原始gRPC请求
            error:  错误异常
            elapsed_ms: 耗时毫秒数

        Returns:
            task_pb2.TaskResp: 错误响应对象
        """
        return task_pb2.TaskResp(
            request_uuid=request.uuid,
            adapter=request.adapter,
            original_request=request,
            effective_url=request.url,
            status_code=500,  # 服务器内部错误
            response_headers={},
            content=b'',
            error_message=str(error),
            response_time_ms=elapsed_ms
        )

    def cleanup(self):
        """
        清理资源

        关闭所有适配器连接，释放资源
        """
        self.logger.info("Cleaning up TaskService resources...")

        for name, adapter in self._adapters.items():
            try:
                adapter.close()
                self.logger.debug(f"Closed adapter: {name}")
            except Exception as e:
                self.logger.warning(f"Error closing adapter {name}: {e}")

        self._adapters.clear()
        self.logger.info("TaskService cleanup completed")

    def get_stats(self) -> Dict[str, Any]:
        """
        获取服务统计信息

        Returns:
            Dict:  统计信息
        """
        return {
            'default_adapter': self.default_adapter,
            'active_adapters': list(self._adapters.keys()),
            'adapter_count': len(self._adapters),
            'config': {
                'adapters': self.adapter_configs,
                'default': self.default_adapter
            }
        }
