import json
import threading
import time
from typing import Any

import grpc
from grpc import ServicerContext
from typing_extensions import override

from ipclick.adapters.base import DownloaderAdapter
from ipclick.adapters.registry import get_adapter, get_default_adapter
from ipclick.dto.models import METHOD_MAP, IPClickAdapter
from ipclick.dto.proto import task_pb2, task_pb2_grpc
from ipclick.dto.response import Response
from ipclick.exceptions import AdapterError, URLNotAllowedError
from ipclick.utils import json_hook
from ipclick.utils.config_util import Settings
from ipclick.utils.log_util import log
from ipclick.utils.url_util import URLPolicy, validate_url


# proto 未设置这些字段时服务端采用的默认值
_DEFAULT_TIMEOUT = 60.0
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_RETRY_BACKOFF = 2.0
_DEFAULT_VERIFY_SSL = True
_DEFAULT_ALLOW_REDIRECTS = True
_DEFAULT_STREAM = False


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
        self._cache_lock = threading.Lock()

        # 目标 URL 准入策略（SSRF 防护）
        self.url_policy: URLPolicy = URLPolicy.from_config(dict(self.config.get("SECURITY", {})))

        # 获取默认适配器
        self.default_adapter: DownloaderAdapter = get_default_adapter()
        self._adapter_cache[self.default_adapter.adapter_name] = self.default_adapter

        # 记录初始化信息
        log.debug(f"TaskService initialized with default adapter: {self.default_adapter.adapter_name}")
        if self.url_policy.block_private_networks:
            log.info("已启用内网地址拦截")
        else:
            log.warning(
                "内网地址拦截未启用；若服务端对不受信任的调用方开放，请设置 [SECURITY].block_private_networks = true"
            )

    # ------------------------------------------------------------------ #
    # 适配器
    # ------------------------------------------------------------------ #

    def _get_cached_adapter(self, name: str) -> DownloaderAdapter:
        """按名称取适配器，并缓存实例。

        原实现只读不写这个缓存，于是每个请求都要新建一次适配器（含
        ``UserAgent()`` 生成器），``cleanup()`` 也永远无事可做。
        """
        adapter = self._adapter_cache.get(name)
        if adapter is not None:
            return adapter

        with self._cache_lock:
            if name not in self._adapter_cache:
                self._adapter_cache[name] = get_adapter(name)
            return self._adapter_cache[name]

    # ------------------------------------------------------------------ #
    # RPC
    # ------------------------------------------------------------------ #

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
        start_time = time.monotonic()
        adapter_name = "unknown"

        try:
            adapter_member = IPClickAdapter.from_pb(request.adapter)
            adapter_name = adapter_member.display_name
            adapter = self._get_cached_adapter(adapter_name)

            validate_url(request.url, self.url_policy)

            response = self._execute_download(adapter, request)
            grpc_response = self._build_grpc_response(request, response)

        except URLNotAllowedError as e:
            log.warning(f"Request {request.uuid} rejected: {e}")
            context.set_code(grpc.StatusCode.PERMISSION_DENIED)
            context.set_details(str(e))
            grpc_response = self._build_error_response(request, str(e))
        except (AdapterError, ValueError) as e:
            log.warning(f"Request {request.uuid} invalid: {e}")
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(str(e))
            grpc_response = self._build_error_response(request, str(e))
        except Exception as e:
            # 任何未预期的异常都不应该让 RPC 以 UNKNOWN + 堆栈的形式返回，
            # 调用方拿不到结构化信息，服务端也可能泄漏内部路径。
            log.exception(f"Request {request.uuid} failed unexpectedly: {e}")
            grpc_response = self._build_error_response(request, f"内部错误: {type(e).__name__}")

        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        grpc_response.response_time_ms = elapsed_ms

        log.info(
            f"Request {request.uuid} completed in {elapsed_ms}ms, "
            f"status: {grpc_response.status_code}, adapter: {adapter_name}"
        )
        return grpc_response

    # ------------------------------------------------------------------ #
    # 下载
    # ------------------------------------------------------------------ #

    @staticmethod
    def _decode_body(raw: str, field_name: str) -> Any:
        """请求体是 JSON 字符串；解析失败时原样透传字符串而不是直接报错。"""
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            log.debug(f"{field_name} 不是合法 JSON，按原始字符串处理")
            return raw

    def _execute_download(self, adapter: DownloaderAdapter, request: task_pb2.ReqTask) -> Response:
        """
        执行下载请求

        Args:
            adapter: HTTP适配器
            request: gRPC请求对象

        Returns:
            Response: 统一响应对象
        """
        method = METHOD_MAP.get(request.method, "GET")

        headers = dict(request.headers) if request.headers else None
        cookies = dict(request.cookies) if request.cookies else None
        extensions = dict(request.extensions) if request.extensions else None

        params = json.loads(request.params, object_hook=json_hook) if request.params else None
        data = self._decode_body(request.data, "data")
        json_data = self._decode_body(request.json, "json")

        # optional 字段：用 HasField 区分"未设置"与"显式设为 0/false"。
        # 这一步是 verify_ssl 默认被关掉那个问题的根因修复。
        download_kwargs: dict[str, Any] = {
            "method": method,
            "headers": headers,
            "cookies": cookies,
            "params": params,
            "data": data,
            "json": json_data,
            "proxy": request.proxy or None,
            "timeout": request.timeout_seconds if request.HasField("timeout_seconds") else _DEFAULT_TIMEOUT,
            "max_retries": request.max_retries if request.HasField("max_retries") else _DEFAULT_MAX_RETRIES,
            "retry_delay": (
                request.retry_backoff_seconds if request.HasField("retry_backoff_seconds") else _DEFAULT_RETRY_BACKOFF
            ),
            "verify": request.verify_ssl if request.HasField("verify_ssl") else _DEFAULT_VERIFY_SSL,
            "allow_redirects": (
                request.allow_redirects if request.HasField("allow_redirects") else _DEFAULT_ALLOW_REDIRECTS
            ),
            "stream": request.stream if request.HasField("stream") else _DEFAULT_STREAM,
            "impersonate": request.impersonate or None,
            "extensions": extensions,
            "automation_config": request.automation_config or None,
            "automation_script": request.automation_script or None,
            "allowed_status_codes": list(request.allowed_status_codes) or None,
            "kwargs": request.kwargs or None,
        }

        # timeout 为 0 会让适配器立刻超时，这里兜底成默认值
        if not download_kwargs["timeout"] or download_kwargs["timeout"] <= 0:
            download_kwargs["timeout"] = _DEFAULT_TIMEOUT

        return adapter.download(request.url, **download_kwargs)

    # ------------------------------------------------------------------ #
    # 响应
    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_grpc_response(request: task_pb2.ReqTask, response: Response) -> task_pb2.TaskResp:
        """
        构建gRPC响应

        Args:
            request: 原始gRPC请求
            response: 统一响应对象

        Returns:
            task_pb2.TaskResp: gRPC响应对象
        """
        return task_pb2.TaskResp(
            request_uuid=request.uuid,
            adapter=request.adapter,
            # 不回传 original_request：它含代理账号密码等凭证，且会让每个响应
            # 白白多带一份完整请求体。
            effective_url=response.url,
            status_code=response.status_code,
            response_headers=response.headers or {},
            content=response.content or b"",
            error_message=str(response.exception) if response.exception else "",
            response_time_ms=response.elapsed_ms,
        )

    @staticmethod
    def _build_error_response(request: task_pb2.ReqTask, message: str) -> task_pb2.TaskResp:
        """构建一个表示失败的响应（状态码 -1，与适配器侧保持一致）。"""
        return task_pb2.TaskResp(
            request_uuid=request.uuid,
            adapter=request.adapter,
            effective_url=request.url,
            status_code=-1,
            error_message=message,
        )

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def cleanup(self) -> None:
        """
        清理资源

        关闭所有适配器连接，释放资源
        """
        log.info("Cleaning up TaskService resources...")

        with self._cache_lock:
            for name, adapter in self._adapter_cache.items():
                try:
                    adapter.close()
                    log.debug(f"Closed adapter: {name}")
                except Exception as e:
                    log.warning(f"Error closing adapter {name}: {e}")
            self._adapter_cache.clear()

        # 注意：不要清空 registry.ADAPTER_CLASSES。那是模块级的类型注册表，
        # 清掉之后同一进程内再也造不出任何适配器（例如测试里起第二个服务）。
        log.info("TaskService cleanup completed")
