import json
import time
from typing import Any

from grpc import ServicerContext
from typing_extensions import override

from ipclick import IPClickAdapter
from ipclick.adapters.base import DownloaderAdapter
from ipclick.adapters.registry import ADAPTER_CLASSES, get_adapter, get_default_adapter
from ipclick.dto import Response
from ipclick.dto.models import METHOD_MAP
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
        # 适配器配置
        self.adapter_config: dict[str, Any] = {
            "DOWNLOADER": self.config.get("DOWNLOADER", {}),
            "BROWSER": self.config.get("BROWSER", {}),
        }

        self._adapter_cache: dict[str, DownloaderAdapter] = {}
        # 获取默认适配器
        self.default_adapter: DownloaderAdapter = get_default_adapter()

        # 记录初始化信息
        log.debug(f"TaskService initialized with default adapter: {self.default_adapter}")

    @override
    def Send(self, request: "task_pb2.ReqTask", context: ServicerContext) -> "task_pb2.TaskResp":
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
        adapter_member = IPClickAdapter.from_pb(request.adapter)
        if adapter_member.display_name not in self._adapter_cache:
            adapter = get_adapter(adapter_member.display_name)
        else:
            adapter = self._adapter_cache[adapter_member.display_name]

        # 执行下载
        response = self._execute_download(adapter, request)

        # 构造gRPC响应
        grpc_response = self._build_grpc_response(request, response)

        # 设置响应时间
        elapsed_ms = int((time.time() - start_time) * 1000)
        grpc_response.response_time_ms = elapsed_ms

        # 记录成功日志
        log.info(
            f"Request {request.uuid} completed in {elapsed_ms}ms, status:  {grpc_response.status_code}, adapter: {adapter_member.display_name}"
        )

        return grpc_response

    def _execute_download(self, adapter: DownloaderAdapter, request: task_pb2.ReqTask) -> Response:
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

        extensions = dict(request.extensions) if request.extensions else None

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
            "extensions": extensions,
            "automation_config": request.automation_config,
            "automation_script": request.automation_script,
            "kwargs": request.kwargs,
        }

        # 构建并验证下载参数
        download_kwargs = self._validate_and_convert_params(download_kwargs)

        # 执行下载
        return adapter.download(request.url, **download_kwargs)

    @staticmethod
    def _validate_and_convert_params(params: dict[str, Any]) -> dict[str, Any]:
        """
        验证并转换参数类型以符合适配器方法的要求
        """
        validated_params: dict[str, Any] = {}

        for key, value in params.items():
            if value is None:
                validated_params[key] = None
                continue

            # 根据参数名称确定期望的类型
            if key == "method":
                validated_params[key] = str(value) if value is not None else None
            elif key in ["headers", "cookies", "params", "data", "json", "extensions"]:
                # 这些应该是字典类型
                if isinstance(value, dict):
                    validated_params[key] = value
                elif value is not None:
                    # 如果不是字典但不为None，则尝试转换或抛出错误
                    raise TypeError(f"Parameter '{key}' must be a dict, got {type(value)}")
                else:
                    validated_params[key] = None
            elif key in ["timeout", "retry_delay"]:
                # 这些应该是浮点数
                validated_params[key] = float(value) if value is not None else None
            elif key in ["max_retries"]:
                # 这些应该是整数
                validated_params[key] = int(value) if value is not None else None
            elif key in ["verify", "allow_redirects", "stream"]:
                # 这些应该是布尔值
                validated_params[key] = bool(value) if value is not None else None
            elif key in ["proxy", "impersonate", "automation_config", "automation_script", "kwargs"]:
                # 这些应该是字符串
                validated_params[key] = str(value) if value is not None else None
            else:
                # 对于其他参数，保持原值
                validated_params[key] = value

        return validated_params

    @staticmethod
    def _build_grpc_response(request: task_pb2.ReqTask, response: Response) -> task_pb2.TaskResp:
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

        for name, adapter in self._adapter_cache.items():
            try:
                adapter.close()
                log.debug(f"Closed adapter: {name}")
            except Exception as e:
                log.warning(f"Error closing adapter {name}: {e}")

        ADAPTER_CLASSES.clear()
        log.info("TaskService cleanup completed")
