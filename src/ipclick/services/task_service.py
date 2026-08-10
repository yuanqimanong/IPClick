from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
import json
import threading
import time
from typing import Any, cast

import grpc
from grpc import ServicerContext
from typing_extensions import override

from ipclick.adapters.base import DEFAULT_CHUNK_SIZE, DownloaderAdapter, StreamEvent, StreamHeader
from ipclick.adapters.browser_settings import BrowserSettings
from ipclick.adapters.registry import get_adapter, get_default_adapter
from ipclick.adapters.settings import AdapterSettings
from ipclick.dto.models import METHOD_MAP, IPClickAdapter
from ipclick.dto.proto import task_pb2, task_pb2_grpc
from ipclick.dto.response import Response
from ipclick.exceptions import AdapterError, URLNotAllowedError
from ipclick.limiter import HostLimiter, HostLimitTimeout, LimiterSettings
from ipclick.metrics import Metrics, get_metrics
from ipclick.utils import json_hook
from ipclick.utils.config_util import Settings
from ipclick.utils.log_util import log
from ipclick.utils.url_util import URLPolicy, validate_url


# proto 未设置这些字段时服务端采用的默认值。
# 超时与重试三项改从 [DOWNLOADER] 配置读取（见 self.adapter_settings），
# 这里只保留没有对应配置项的布尔默认值。
_DEFAULT_VERIFY_SSL = True
_DEFAULT_ALLOW_REDIRECTS = True
_DEFAULT_STREAM = False


class _IsolatedContext:
    """批量里给单个任务用的假 ServicerContext。

    只吞掉状态码设置，不影响批量共享的那条流。
    """

    def set_code(self, _code: object) -> None: ...

    def set_details(self, _details: str) -> None: ...

    def is_active(self) -> bool:
        return True


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

        # 适配器默认行为。此前这里只是把 [DOWNLOADER] 存进一个再没被读过的
        # dict，导致配置文件里的超时与重试策略完全不生效。
        self.adapter_settings: AdapterSettings = AdapterSettings.from_config(dict(self.config.get("DOWNLOADER", {})))
        # 同理，[BROWSER] 此前也没有任何消费方
        self.browser_settings: BrowserSettings = BrowserSettings.from_config(dict(self.config.get("BROWSER", {})))

        self._adapter_cache: dict[str, DownloaderAdapter] = {}
        self._cache_lock: threading.Lock = threading.Lock()
        self._metrics: Metrics = get_metrics()

        downloader_config = dict(self.config.get("DOWNLOADER", {}))
        self._chunk_size: int = int(downloader_config.get("chunk_size", DEFAULT_CHUNK_SIZE) or DEFAULT_CHUNK_SIZE)
        # 批量的并发度沿用 SERVER.max_workers：这里再开一个不受约束的池的话，
        # 总并发会变成 max_workers x 批量数，把下游打爆。
        self._batch_concurrency: int = max(1, int(dict(self.config.get("SERVER", {})).get("max_workers", 10) or 10))

        # 目标 URL 准入策略（SSRF 防护）
        self.url_policy: URLPolicy = URLPolicy.from_config(dict(self.config.get("SECURITY", {})))

        # 按 host 的并发与速率闸门。未配置时是零开销的空操作。
        self.host_limiter: HostLimiter = HostLimiter(LimiterSettings.from_config(downloader_config))

        # 获取默认适配器
        self.default_adapter: DownloaderAdapter = get_default_adapter(self.adapter_settings)
        self._adapter_cache[self.default_adapter.adapter_name] = self.default_adapter

        # 记录初始化信息
        log.debug(
            f"TaskService initialized with default adapter: {self.default_adapter.adapter_name}, "
            f"timeout={self.adapter_settings.download_timeout}s, "
            f"max_attempts={self.adapter_settings.max_attempts}, "
            f"retry_codes={sorted(self.adapter_settings.retry_codes)}"
        )
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
                self._adapter_cache[name] = get_adapter(name, self.adapter_settings, self.browser_settings)
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
        method_name = METHOD_MAP.get(request.method, "GET")

        # 适配器名必须在进入 track_request 之前解析：上下文管理器在进入时就
        # 固定了标签值，放在 try 里面赋值的话所有指标都会记成 "unknown"。
        # 枚举值非法时用 "unknown" 是正确的——下面的 from_pb 会抛错并记为拒绝。
        try:
            adapter_name = IPClickAdapter.from_pb(request.adapter).display_name
        except ValueError:
            adapter_name = "unknown"

        # 注意：指标标签里绝不放 URL 或目标主机——爬虫场景下那是无界基数，
        # 会把 Prometheus 撑爆，同时也等于在指标端点上公开抓取目标。
        with self._metrics.track_request(adapter_name, method_name) as metric_ctx:
            try:
                adapter_member = IPClickAdapter.from_pb(request.adapter)
                adapter_name = adapter_member.display_name
                adapter = self._get_cached_adapter(adapter_name)

                validate_url(request.url, self.url_policy)

                response = self._execute_download(adapter, request)
                grpc_response = self._build_grpc_response(request, response)

            except URLNotAllowedError as e:
                log.warning(f"Request {request.uuid} rejected: {e}")
                self._metrics.record_rejected("url_not_allowed")
                context.set_code(grpc.StatusCode.PERMISSION_DENIED)
                context.set_details(str(e))
                grpc_response = self._build_error_response(request, str(e))
            except HostLimitTimeout as e:
                # 本机限流策略生效，不是目标站点或网络的问题。RESOURCE_EXHAUSTED
                # 是 gRPC 里表达"被限流了，稍后再来"的标准状态码。
                log.warning(f"Request {request.uuid} throttled: {e}")
                self._metrics.record_rejected("host_limit")
                context.set_code(grpc.StatusCode.RESOURCE_EXHAUSTED)
                context.set_details(str(e))
                grpc_response = self._build_error_response(request, str(e))
            except AdapterError as e:
                # 与 ValueError 分开：这是"本服务端做不到"（适配器不存在、依赖没装、
                # 浏览器渲染被关掉），不是调用方参数写错。混成 INVALID_ARGUMENT
                # 会让调用方去改自己的参数，而实际要改的是服务端部署。
                log.warning(f"Request {request.uuid} cannot be served: {e}")
                self._metrics.record_rejected("failed_precondition")
                context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
                context.set_details(str(e))
                grpc_response = self._build_error_response(request, str(e))
            except ValueError as e:
                log.warning(f"Request {request.uuid} invalid: {e}")
                self._metrics.record_rejected("invalid_argument")
                context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
                context.set_details(str(e))
                grpc_response = self._build_error_response(request, str(e))
            except Exception as e:
                # 任何未预期的异常都不应该让 RPC 以 UNKNOWN + 堆栈的形式返回，
                # 调用方拿不到结构化信息，服务端也可能泄漏内部路径。
                log.exception(f"Request {request.uuid} failed unexpectedly: {e}")
                self._metrics.record_rejected("internal_error")
                grpc_response = self._build_error_response(request, f"内部错误: {type(e).__name__}")

            metric_ctx["status_code"] = grpc_response.status_code
            metric_ctx["size"] = len(grpc_response.content)

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

    def _build_download_kwargs(self, request: task_pb2.ReqTask) -> dict[str, Any]:
        """把 protobuf 请求翻译成适配器参数。

        单请求与流式两条路径共用，避免默认值处理（尤其是 proto3 显式存在性
        那套逻辑）在两处各写一份而失步。
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
            # 未设置时回落到 [DOWNLOADER] 配置，而不是写死的常量
            "timeout": (
                request.timeout_seconds
                if request.HasField("timeout_seconds")
                else self.adapter_settings.download_timeout
            ),
            "max_retries": (
                request.max_retries if request.HasField("max_retries") else self.adapter_settings.max_attempts
            ),
            "retry_delay": (
                request.retry_backoff_seconds
                if request.HasField("retry_backoff_seconds")
                else self.adapter_settings.initial_backoff
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
            download_kwargs["timeout"] = self.adapter_settings.download_timeout

        return download_kwargs

    def _execute_download(self, adapter: DownloaderAdapter, request: task_pb2.ReqTask) -> Response:
        """执行一次（非流式）下载。"""
        with self.host_limiter.acquire(request.url):
            return adapter.download(request.url, **self._build_download_kwargs(request))

    def _limited_stream(self, url: str, stream: Iterator[StreamEvent]) -> Iterator[StreamEvent]:
        """给流式下载套上 host 限流，并保证底层生成器最终被关闭。

        额度在第一次取值时获取（此时才真正发出请求），随 RPC 结束一并释放。
        """
        with self.host_limiter.acquire(url):
            try:
                yield from stream
            finally:
                # 提前 break（调用方断开）时也要让适配器释放底层连接。
                # download_stream 的签名只承诺 Iterator，不一定是生成器，
                # 所以这里探测一下再调，不能直接用 contextlib.closing。
                closer = getattr(stream, "close", None)
                if callable(closer):
                    closer()

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
    # 流式下载
    # ------------------------------------------------------------------ #

    @override
    def SendStream(self, request: "task_pb2.ReqTask", context: ServicerContext) -> Iterator["task_pb2.TaskRespChunk"]:
        """流式下载：先发 header，再分片发 body，最后发 trailer。

        与 Send 的关键区别是响应体不会在服务端整个进内存——对大文件来说，
        Send 会把 body 复制好几份（适配器的 content、protobuf 序列化缓冲、
        gRPC 发送缓冲），而这里始终只有一个分片在内存里。
        """
        log.info(f"Received stream request: {request.uuid} for URL: {request.url}")
        start_time = time.monotonic()
        total_bytes = 0
        error_message = ""

        try:
            adapter_name = IPClickAdapter.from_pb(request.adapter).display_name
        except ValueError:
            adapter_name = "unknown"

        with self._metrics.track_request(adapter_name, METHOD_MAP.get(request.method, "GET")) as metric_ctx:
            try:
                adapter = self._get_cached_adapter(adapter_name)
                validate_url(request.url, self.url_policy)

                download_kwargs = self._build_download_kwargs(request)
                # 流式路径不做重试：重试要么得缓存已发出的分片、要么让调用方
                # 看到重复数据，两者都不可接受。
                download_kwargs.pop("max_retries", None)
                download_kwargs.pop("retry_delay", None)

                # 限流额度要持有到整条流结束：流式请求同样占着一条到该 host 的连接，
                # 只在建流时占额度等于没限住。
                stream = self._limited_stream(
                    request.url,
                    adapter.download_stream(request.url, chunk_size=self._chunk_size, **download_kwargs),
                )

                header_sent = False
                for event in stream:
                    if context.is_active() is False:  # 调用方已断开，别再白白下载
                        log.info(f"Stream request {request.uuid} cancelled by client")
                        break

                    if isinstance(event, StreamHeader):
                        metric_ctx["status_code"] = event.status_code
                        error_message = event.error or ""
                        yield task_pb2.TaskRespChunk(
                            header=task_pb2.TaskRespHeader(
                                request_uuid=request.uuid,
                                adapter=request.adapter,
                                effective_url=event.url,
                                status_code=event.status_code,
                                response_headers=event.headers or {},
                                error_message=error_message,
                                content_length=event.content_length,
                            )
                        )
                        header_sent = True
                        if event.error:
                            break
                    else:
                        total_bytes += len(event)
                        yield task_pb2.TaskRespChunk(chunk=event)

                if not header_sent:
                    # 适配器一个事件都没产出——不该发生，但别让调用方收到一个
                    # 没有 header 的流而无从判断。
                    error_message = "适配器未返回任何响应"
                    yield task_pb2.TaskRespChunk(
                        header=task_pb2.TaskRespHeader(
                            request_uuid=request.uuid,
                            adapter=request.adapter,
                            effective_url=request.url,
                            status_code=-1,
                            error_message=error_message,
                        )
                    )

            except URLNotAllowedError as e:
                error_message = str(e)
                self._metrics.record_rejected("url_not_allowed")
                context.set_code(grpc.StatusCode.PERMISSION_DENIED)
                context.set_details(error_message)
                yield self._stream_error_header(request, error_message)
            except HostLimitTimeout as e:
                error_message = str(e)
                self._metrics.record_rejected("host_limit")
                context.set_code(grpc.StatusCode.RESOURCE_EXHAUSTED)
                context.set_details(error_message)
                yield self._stream_error_header(request, error_message)
            except AdapterError as e:
                error_message = str(e)
                self._metrics.record_rejected("failed_precondition")
                context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
                context.set_details(error_message)
                yield self._stream_error_header(request, error_message)
            except ValueError as e:
                error_message = str(e)
                self._metrics.record_rejected("invalid_argument")
                context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
                context.set_details(error_message)
                yield self._stream_error_header(request, error_message)
            except Exception as e:
                log.exception(f"Stream request {request.uuid} failed: {e}")
                self._metrics.record_rejected("internal_error")
                error_message = f"内部错误: {type(e).__name__}"
                yield self._stream_error_header(request, error_message)

            metric_ctx["size"] = total_bytes

        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        yield task_pb2.TaskRespChunk(
            trailer=task_pb2.TaskRespTrailer(
                response_time_ms=elapsed_ms,
                total_bytes=total_bytes,
                error_message=error_message,
            )
        )
        log.info(f"Stream request {request.uuid} finished in {elapsed_ms}ms, {total_bytes} bytes")

    @staticmethod
    def _stream_error_header(request: task_pb2.ReqTask, message: str) -> "task_pb2.TaskRespChunk":
        return task_pb2.TaskRespChunk(
            header=task_pb2.TaskRespHeader(
                request_uuid=request.uuid,
                adapter=request.adapter,
                effective_url=request.url,
                status_code=-1,
                error_message=message,
            )
        )

    # ------------------------------------------------------------------ #
    # 批量
    # ------------------------------------------------------------------ #

    @override
    def SendBatch(
        self, request_iterator: Iterator["task_pb2.ReqTask"], context: ServicerContext
    ) -> Iterator["task_pb2.TaskResp"]:
        """批量：客户端流式推任务，服务端按**完成顺序**流式返回。

        并发度受 [SERVER].max_workers 约束——这里再开一个自己的线程池的话，
        总并发会变成 max_workers × 批量数，把下游打爆。用 as_completed 让快的
        先返回，而不是按提交顺序阻塞在慢请求上。
        """
        pending: dict[Future[task_pb2.TaskResp], str] = {}
        submitted = 0

        with ThreadPoolExecutor(max_workers=self._batch_concurrency, thread_name_prefix="ipclick-batch") as pool:
            for request in request_iterator:
                if not context.is_active():
                    log.info("Batch cancelled by client")
                    break
                future = pool.submit(self._handle_one, request)
                pending[future] = request.uuid
                submitted += 1

                # 边收边发：不等客户端把所有任务推完再开始返回结果，
                # 否则一个超长的批次会让首个结果迟迟不到。
                done = [f for f in pending if f.done()]
                for f in done:
                    del pending[f]
                    yield f.result()

            for future in as_completed(list(pending)):
                if not context.is_active():
                    break
                yield future.result()

        log.info(f"Batch finished, {submitted} tasks")

    def _handle_one(self, request: "task_pb2.ReqTask") -> "task_pb2.TaskResp":
        """批量里的单个任务，复用 Send 的全部逻辑（含指标与安全校验）。

        刻意传一个隔离的假 context：Send 在出错时会调 set_code/set_details，
        若把批量共享的那个 context 传进去，一个任务的 URL 被 SSRF 拦截就会把
        **整条批量流**标记成 PERMISSION_DENIED，其余任务的结果全部丢失。
        每个任务的失败信息已经在它自己的 TaskResp.error_message 里了。
        """
        # _IsolatedContext 只实现了 Send 实际用到的三个方法，
        # 不是完整的 ServicerContext，这里显式 cast。
        return self.Send(request, cast(ServicerContext, cast(object, _IsolatedContext())))

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
