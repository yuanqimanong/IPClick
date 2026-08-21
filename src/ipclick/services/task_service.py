"""同步任务 RPC 的请求校验、执行、流式传输、批处理与资源清理。"""

from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
import json
import math
import queue
import threading
import time
from typing import Any, cast

from grpc import ServicerContext
from typing_extensions import override

from ipclick.adapters.base import DEFAULT_CHUNK_SIZE, DownloaderAdapter, StreamEvent, StreamHeader
from ipclick.adapters.browser_settings import BrowserSettings
from ipclick.adapters.registry import (
    GENERIC_BROWSER_NAME,
    get_adapter,
    get_default_adapter,
    resolve_browser_adapter_name,
)
from ipclick.adapters.retry import caller_alive_check
from ipclick.adapters.settings import HARD_MAX_RETRIES, AdapterSettings
from ipclick.dto.models import METHOD_MAP, IPClickAdapter
from ipclick.dto.proto import task_pb2, task_pb2_grpc
from ipclick.dto.response import Response
from ipclick.exceptions import ValidationError
from ipclick.limiter import HostLimiter, build_limiter
from ipclick.protocols import ShardableLimiter
from ipclick.server_settings import ServerSettings
from ipclick.services.components import ComponentService
from ipclick.services.detached import DetachedContext
from ipclick.services.errors import CallerGone, classify, report
from ipclick.trace import RequestTrace, TraceRecorder, get_recorder
from ipclick.utils.coerce import as_int
from ipclick.utils.config_util import Settings, section
from ipclick.utils.log_util import log
from ipclick.utils.url_util import URLPolicy, validate_url


_DEFAULT_VERIFY_SSL = True
_DEFAULT_ALLOW_REDIRECTS = True
_DEFAULT_STREAM = False


def caller_still_waiting(context: object) -> bool:
    """兼容真实/内部上下文地判断调用方是否仍在等待。"""
    checker = getattr(context, "is_active", None)
    try:
        if callable(checker):
            return bool(checker())
        # grpc.aio.ServicerContext 没有 is_active()，以 cancelled() 表示对端状态。
        cancelled = getattr(context, "cancelled", None)
        return not bool(cancelled()) if callable(cancelled) else True
    except Exception:
        return True


FORWARD_HEADER = "ipclick-forwarded"


def is_forwarded(context: object) -> bool:
    """检查内部一跳转发标记，防止服务端之间形成派发环路。"""
    getter = cast(Callable[[], Any] | None, getattr(context, "invocation_metadata", None))
    if not callable(getter):
        return False
    try:
        metadata: Any = getter() or ()
        return any(str(key).lower() == FORWARD_HEADER for key, _value in metadata)
    except Exception:
        return False


def _adapter_display_name(pb_value: int) -> str:
    try:
        return IPClickAdapter.from_pb(pb_value).display_name
    except ValueError:
        return "unknown"


def _build_trace(tr: RequestTrace | None) -> "task_pb2.Trace | None":
    if tr is None:
        return None
    return task_pb2.Trace(
        node_id=tr.node_id,
        adapter=tr.adapter,
        attempts=tr.attempts,
        forwarded=tr.forwarded,
        queued_ms=tr.queued_ms,
    )


class TaskService(task_pb2_grpc.TaskServiceServicer):
    """实现下载、流式下载、批量任务、探测和组件管理 RPC。"""

    def __init__(self, config: Settings):
        self.config: Settings = config

        self.adapter_settings: AdapterSettings = AdapterSettings.from_config(section(self.config, "DOWNLOADER"))
        self.browser_settings: BrowserSettings = BrowserSettings.from_config(section(self.config, "BROWSER"))

        self._adapter_cache: dict[str, DownloaderAdapter] = {}
        self._cache_lock: threading.Lock = threading.Lock()
        self._recorder: TraceRecorder = get_recorder()
        self.node_id: str = str(section(self.config, "CLUSTER").get("self_id", "") or "") or (self._recorder.node_id)

        downloader_config = section(self.config, "DOWNLOADER")
        # 走 as_int 而不是裸 int()：写成非数字会在**构造期**抛 ValueError（服务端起不来，
        # 报错也不指向这一项），而负数或 0 更糟——流式响应会静默返回 0 字节且 status=200。
        # as_int 越界即回落默认值，配合下面的告警让人知道自己写错了。
        configured_chunk = downloader_config.get("chunk_size", DEFAULT_CHUNK_SIZE)
        self._chunk_size: int = as_int(configured_chunk, DEFAULT_CHUNK_SIZE, minimum=1)
        if configured_chunk is not None and self._chunk_size != configured_chunk:
            log.warning(
                f"[DOWNLOADER].chunk_size={configured_chunk!r} 不是 >= 1 的整数，"
                f"改用默认值 {DEFAULT_CHUNK_SIZE}"
            )
        self._batch_concurrency: int = ServerSettings.from_config(section(self.config, "SERVER")).max_workers
        self._batch_executor: ThreadPoolExecutor | None = None
        self._started_at: float = time.monotonic()
        self.components: ComponentService = ComponentService(self.config, _refresh_registry_after_install)

        self.url_policy: URLPolicy = URLPolicy.from_config(section(self.config, "SECURITY"))

        self.host_limiter: HostLimiter = build_limiter(downloader_config)

        self.default_adapter: DownloaderAdapter = get_default_adapter(self.adapter_settings)
        self._adapter_cache[self.default_adapter.adapter_name] = self.default_adapter

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

    def limiters_for_sharding(self) -> list[ShardableLimiter]:
        """返回可按健康节点数分摊配额的限流器。"""
        return [self.host_limiter]

    def _cache_key(self, name: str) -> str:
        if name == GENERIC_BROWSER_NAME:
            try:
                return resolve_browser_adapter_name(self.browser_settings)
            except Exception:
                return name
        return name

    def _get_cached_adapter(self, name: str) -> DownloaderAdapter:
        """线程安全地惰性创建并复用下载适配器。"""
        key = self._cache_key(name)
        adapter = self._adapter_cache.get(key)
        if adapter is not None:
            return adapter

        with self._cache_lock:
            if key not in self._adapter_cache:
                created = get_adapter(name, self.adapter_settings, self.browser_settings)
                # 注入逐跳校验器：准入只看入口 URL，重定向目标必须在发出之前再过一遍，
                # 否则一次 302 就能跳到云元数据地址而完全绕过 [SECURITY] 策略。
                created.url_validator = self._validate_redirect_target
                self._adapter_cache[key] = created
            return self._adapter_cache[key]

    def _validate_redirect_target(self, url: str) -> None:
        """校验重定向目标；不被允许时抛 ``URLNotAllowedError``。"""
        validate_url(url, self.url_policy)

    @override
    def Send(self, request: "task_pb2.ReqTask", context: ServicerContext) -> "task_pb2.TaskResp":
        """校验并执行单个下载，将可预期异常映射为稳定响应。"""
        started = time.monotonic()
        with self.track(request, context) as tr:
            try:
                adapter = self.prepare(request, context, tr)
                response = self._execute_download(adapter, request, tr, context)
                grpc_response = self.accept(request, response, tr)
            except Exception as e:
                grpc_response = self._response_for_exception(e, request, tr, context)
            self.record_outcome(tr, grpc_response)
        return self.stamp_elapsed(grpc_response, started, request)

    @contextmanager
    def track(self, request: "task_pb2.ReqTask", context: ServicerContext, *, stream: bool = False):
        """创建请求追踪并补充节点、方法和转发来源。"""
        log.debug("Received request: {} for URL: {}", request.uuid, request.url)
        with self._recorder.track_request(
            _adapter_display_name(request.adapter),
            METHOD_MAP.get(request.method, "GET"),
            uuid=request.uuid,
            url=request.url,
            stream=stream,
        ) as tr:
            tr.node_id = self.node_id
            tr.forwarded = is_forwarded(context)
            yield tr

    def prepare(self, request: "task_pb2.ReqTask", context: ServicerContext, tr: RequestTrace) -> DownloaderAdapter:
        """解析适配器、验证 URL 策略，并在执行前检查调用方状态。"""
        if request.method not in METHOD_MAP:
            raise ValueError(f"未知的 HTTP 方法枚举值: {request.method}")
        adapter = self._get_cached_adapter(_adapter_display_name(request.adapter))
        tr.adapter = adapter.adapter_name
        validate_url(request.url, self.url_policy)
        if not caller_still_waiting(context):
            raise CallerGone
        return adapter

    def accept(self, request: "task_pb2.ReqTask", response: Response, tr: RequestTrace) -> "task_pb2.TaskResp":
        """将适配器响应及尝试次数转换为 RPC 响应。"""
        tr.attempts = response.attempts
        return self._build_grpc_response(request, response, tr)

    @staticmethod
    def record_outcome(tr: RequestTrace, grpc_response: "task_pb2.TaskResp") -> None:
        """把最终状态、大小和错误写入请求追踪。"""
        tr.status_code = grpc_response.status_code
        tr.size = len(grpc_response.content)
        tr.error = grpc_response.error_message

    @staticmethod
    def stamp_elapsed(
        grpc_response: "task_pb2.TaskResp", started: float, request: "task_pb2.ReqTask"
    ) -> "task_pb2.TaskResp":
        """写入服务端观测到的总耗时并返回原响应。"""
        grpc_response.response_time_ms = int((time.monotonic() - started) * 1000)
        log.debug(
            "Request {} completed in {}ms, status: {}",
            request.uuid,
            grpc_response.response_time_ms,
            grpc_response.status_code,
        )
        return grpc_response

    def _response_for_exception(
        self, exc: Exception, request: "task_pb2.ReqTask", tr: RequestTrace, context: ServicerContext
    ) -> "task_pb2.TaskResp":
        failure = classify(exc)
        report(failure, exc, request_uuid=request.uuid, recorder=self._recorder, context=context)
        return self._build_error_response(request, failure.message, tr)

    @staticmethod
    def _decode_body(raw: str | bytes, field_name: str) -> Any:
        if not raw:
            return None
        if isinstance(raw, bytes):
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                log.debug(f"{field_name} 不是 UTF-8，按二进制原样透传（{len(raw)} 字节）")
                return raw
        else:
            text = raw
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            log.debug(f"{field_name} 不是合法 JSON，按原始字符串处理")
            return text

    def _build_download_kwargs(self, request: task_pb2.ReqTask) -> dict[str, Any]:
        """将 protobuf 可选字段转换为适配器调用参数。"""
        method = METHOD_MAP.get(request.method, "GET")

        if request.HasField("max_retries") and not 0 <= request.max_retries <= HARD_MAX_RETRIES:
            raise ValidationError(f"max_retries 必须在 0 到 {HARD_MAX_RETRIES} 之间")
        if request.HasField("timeout_seconds") and not math.isfinite(request.timeout_seconds):
            raise ValidationError("timeout_seconds 必须是有限数字")
        if request.HasField("retry_backoff_seconds") and (
            not math.isfinite(request.retry_backoff_seconds) or request.retry_backoff_seconds < 0
        ):
            raise ValidationError("retry_backoff_seconds 必须是有限的非负数")

        headers = dict(request.headers) if request.headers else None
        cookies = dict(request.cookies) if request.cookies else None

        # 查询参数**不过** json_hook：它会把形似 ISO 的字符串还原成 datetime，
        # 而这些值最终要被 str() 拼进 URL。于是 params={"start": "2024-01-01"} 发出去
        # 变成 start=2024-01-01+00:00:00，目标 API 的日期过滤直接失效或返回 400，
        # 而调用方从自己传的值里完全看不出问题。Python 3.11 起 fromisoformat 更宽松，
        # "20241231" 这种紧凑写法也会被吃掉。
        params = json.loads(request.params) if request.params else None
        data = self._decode_body(request.data, "data")
        json_data = self._decode_body(request.json, "json")

        download_kwargs: dict[str, Any] = {
            "method": method,
            "headers": headers,
            "cookies": cookies,
            "params": params,
            "data": data,
            "json": json_data,
            "proxy": request.proxy or None,
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
            "automation_config": request.automation_config or None,
            "automation_script": request.automation_script or None,
            "allowed_status_codes": list(request.allowed_status_codes) or None,
            "kwargs": request.kwargs or None,
        }

        if not download_kwargs["timeout"] or download_kwargs["timeout"] <= 0:
            download_kwargs["timeout"] = self.adapter_settings.download_timeout

        return download_kwargs

    def _execute_download(
        self,
        adapter: DownloaderAdapter,
        request: task_pb2.ReqTask,
        tr: RequestTrace | None = None,
        context: ServicerContext | None = None,
    ) -> Response:
        waiting_since = time.monotonic()
        with self.host_limiter.acquire(request.url):
            if tr is not None:
                tr.queued_ms = int((time.monotonic() - waiting_since) * 1000)
            # 把"调用方还在不在"带进重试循环：重试会把耗时按尝试次数放大，
            # deadline 早过了还在对目标站点重投等于凭空放大请求、还占着线程。
            token = caller_alive_check.set(None if context is None else lambda: caller_still_waiting(context))
            try:
                return adapter.download(request.url, **self._build_download_kwargs(request))
            finally:
                caller_alive_check.reset(token)

    def _limited_stream(self, url: str, stream: Iterator[StreamEvent]) -> Iterator[StreamEvent]:
        """在整个响应流生命周期内持有 host 限流槽并保证关闭流。"""
        with self.host_limiter.acquire(url):
            yield from self._closing_stream(stream)

    @staticmethod
    def _closing_stream(stream: Iterator[StreamEvent]) -> Iterator[StreamEvent]:
        """推进响应流，并保证无论如何都关掉底层 HTTP 流。"""
        try:
            yield from stream
        finally:
            closer = getattr(stream, "close", None)
            if callable(closer):
                closer()

    @staticmethod
    def _build_grpc_response(
        request: task_pb2.ReqTask, response: Response, tr: RequestTrace | None = None
    ) -> task_pb2.TaskResp:
        return task_pb2.TaskResp(
            request_uuid=request.uuid,
            adapter=request.adapter,
            effective_url=response.url,
            status_code=response.status_code,
            response_headers=response.headers or {},
            content=response.content or b"",
            error_message=str(response.exception) if response.exception else "",
            response_time_ms=response.elapsed_ms,
            trace=_build_trace(tr),
        )

    @staticmethod
    def _build_error_response(
        request: task_pb2.ReqTask, message: str, tr: RequestTrace | None = None
    ) -> task_pb2.TaskResp:
        return task_pb2.TaskResp(
            request_uuid=request.uuid,
            adapter=request.adapter,
            effective_url=request.url,
            status_code=-1,
            error_message=message,
            trace=_build_trace(tr),
        )

    @override
    def SendStream(self, request: "task_pb2.ReqTask", context: ServicerContext) -> Iterator["task_pb2.TaskRespChunk"]:
        """依次发送 header、内容块和 trailer，异常也保持协议结构完整。"""
        started = time.monotonic()
        total_bytes = 0
        error_message = ""

        with self.track(request, context, stream=True) as tr:
            header_sent = False
            try:
                adapter = self.prepare(request, context, tr)
                for event in self._open_stream(adapter, request):
                    if not caller_still_waiting(context):
                        log.info(f"Stream request {request.uuid} cancelled by client")
                        break
                    if isinstance(event, StreamHeader):
                        tr.status_code = event.status_code
                        error_message = event.error or ""
                        header_sent = True
                        yield self._stream_header(request, event)
                        if event.error:
                            break
                    else:
                        total_bytes += len(event)
                        yield task_pb2.TaskRespChunk(chunk=event)

                if not header_sent:
                    error_message = "适配器未返回任何响应"
                    yield self._stream_error_header(request, error_message)

            except Exception as e:
                failure = classify(e)
                report(failure, e, request_uuid=request.uuid, recorder=self._recorder, context=context)
                error_message = failure.message
                # header 已发送后只能通过 trailer 报告正文阶段错误；再次发送 header
                # 会破坏 header -> chunks -> trailer 的协议结构。
                if not header_sent:
                    yield self._stream_error_header(request, error_message)

            tr.size = total_bytes
            tr.error = error_message
            if error_message and tr.status_code < 0:
                tr.status_code = -1

        elapsed_ms = int((time.monotonic() - started) * 1000)
        yield task_pb2.TaskRespChunk(
            trailer=task_pb2.TaskRespTrailer(
                response_time_ms=elapsed_ms,
                total_bytes=total_bytes,
                error_message=error_message,
            )
        )
        log.debug("Stream request {} finished in {}ms, {} bytes", request.uuid, elapsed_ms, total_bytes)

    def _open_stream(self, adapter: DownloaderAdapter, request: "task_pb2.ReqTask") -> Iterator[StreamEvent]:
        """打开响应流；重试参数按调用方要求原样透传。

        这里曾经把 ``max_retries`` / ``retry_delay`` 抹掉，理由写的是"避免已发送内容被
        重复"。但那个理由对现有的两条流式路径都不成立：

        - 真流式的适配器（curl_cffi、niquests 重写了 ``download_stream``）**没有**被
          ``@retry()`` 装饰、也不读 ``max_retries``，抹不抹都没有区别；
        - 其余适配器（浏览器系、DrissionPage）走基类兜底实现，那是"先整体下载完再切块"，
          ``download()`` 在第一个分片被 yield 之前就已经跑完——重试全部发生在任何字节
          到达调用方之前，不存在重复发送。

        抹掉的实际后果只有一个：调用方传的 ``max_retries`` 被静默忽略。所以现在原样透传。

        真流式适配器若将来加上 ``@retry()``，就必须在那一层自己判断"已经吐过字节了
        就不能重投"，而不是靠这里抹参数——那样做只会让调用方的参数看起来生效实际不生效。
        """
        download_kwargs = self._build_download_kwargs(request)
        return self._limited_stream(
            request.url,
            adapter.download_stream(request.url, chunk_size=self._chunk_size, **download_kwargs),
        )

    @staticmethod
    def _stream_header(request: "task_pb2.ReqTask", event: StreamHeader) -> "task_pb2.TaskRespChunk":
        return task_pb2.TaskRespChunk(
            header=task_pb2.TaskRespHeader(
                request_uuid=request.uuid,
                adapter=request.adapter,
                effective_url=event.url,
                status_code=event.status_code,
                response_headers=event.headers or {},
                error_message=event.error or "",
                content_length=event.content_length,
            )
        )

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

    @override
    def SendBatch(
        self, request_iterator: Iterator["task_pb2.ReqTask"], context: ServicerContext
    ) -> Iterator["task_pb2.TaskResp"]:
        """在线程池并发执行客户端流，并按完成顺序返回结果。"""
        pool = self._batch_pool()
        finished: queue.Queue[Future[task_pb2.TaskResp]] = queue.Queue()
        in_flight = 0
        submitted = 0
        # 记下所有已提交的 future：调用方中途断开时，还排在队列里没开始跑的那些必须
        # 取消掉。之前只是 break 出循环，已提交的任务照样一个个执行完——子任务用的是
        # DetachedContext，它的 is_active() 恒为 True，所以它们也没法自己放弃。
        # 结果是调用方早就走了，服务端还在替它把整批请求打给目标站点。
        pending: list[Future[task_pb2.TaskResp]] = []

        for request in request_iterator:
            if not context.is_active():
                log.info("Batch cancelled by client")
                break
            future: Future[task_pb2.TaskResp] = pool.submit(self._handle_one, request, batch_metadata(context))
            pending.append(future)
            future.add_done_callback(finished.put)
            in_flight += 1
            submitted += 1

            if in_flight >= self._batch_concurrency:
                # ThreadPoolExecutor 的内部队列无界；在 worker 数处施加背压，
                # 防止超长客户端流把全部任务和请求体堆进内存。
                done = finished.get()
                in_flight -= 1
                yield done.result()

            while True:
                try:
                    done = finished.get_nowait()
                except queue.Empty:
                    break
                in_flight -= 1
                yield done.result()

        while in_flight > 0:
            if not context.is_active():
                break
            done = finished.get()
            in_flight -= 1
            yield done.result()

        cancelled = sum(1 for future in pending if future.cancel())
        if cancelled:
            log.info(f"调用方已断开，取消了 {cancelled} 个尚未开始的批量子任务")
        log.info(f"Batch finished, {submitted} tasks")

    def _batch_pool(self) -> ThreadPoolExecutor:
        """惰性创建服务级批处理线程池。"""
        pool = self._batch_executor
        if pool is not None:
            return pool
        with self._cache_lock:
            if self._batch_executor is None:
                self._batch_executor = ThreadPoolExecutor(
                    max_workers=self._batch_concurrency, thread_name_prefix="ipclick-batch"
                )
            return self._batch_executor

    def _handle_one(
        self, request: "task_pb2.ReqTask", metadata: tuple[tuple[str, str], ...] = ()
    ) -> "task_pb2.TaskResp":
        return self.Send(request, DetachedContext(metadata).as_servicer_context())

    @override
    def Ping(self, request: "task_pb2.PingReq", context: ServicerContext) -> "task_pb2.PingResp":
        """返回节点身份、能力和当前在途请求数。"""
        _ = context
        if request.from_node:
            log.debug(f"收到来自节点 {request.from_node} 的探测")
        return task_pb2.PingResp(
            node_id=self.node_id,
            version=_version(),
            auth_required=self.auth_required,
            forward=self.forward_enabled,
            uptime_seconds=int(time.monotonic() - self._started_at),
            in_flight=self._recorder.counters.in_flight,
        )

    @property
    def remote_install_allowed(self) -> bool:
        """返回当前节点是否允许远程管理可选组件。"""
        return self.components.enabled

    @override
    def Component(self, request: "task_pb2.ComponentReq", context: ServicerContext) -> "task_pb2.ComponentResp":
        """将组件管理请求交给受配置保护的子服务。"""
        return self.components.handle(request, context, node_id=self.node_id)

    @property
    def auth_required(self) -> bool:
        """返回当前配置是否要求公共或集群令牌。"""
        from ipclick.auth import load_tokens
        from ipclick.cluster.tokens import cluster_secret

        security = section(self.config, "SECURITY")
        return bool(load_tokens(security)) or bool(cluster_secret(section(self.config, "CLUSTER")))

    @property
    def forward_enabled(self) -> bool:
        """普通任务服务不执行服务端转发。"""
        return False

    def cleanup(self) -> None:
        """停止批处理并同步关闭所有已实例化适配器。"""
        log.info("Cleaning up TaskService resources...")
        self._shutdown_batch_pool()
        for name, adapter in self._drain_adapters():
            try:
                adapter.close()
                log.debug(f"Closed adapter: {name}")
            except Exception as e:
                log.warning(f"Error closing adapter {name}: {e}")
        log.info("TaskService cleanup completed")

    async def acleanup(self) -> None:
        """停止批处理并异步关闭所有已实例化适配器。"""
        log.info("Cleaning up TaskService resources...")
        self._shutdown_batch_pool()
        for name, adapter in self._drain_adapters():
            try:
                await adapter.aclose()
                log.debug(f"Closed adapter: {name}")
            except Exception as e:
                log.warning(f"Error closing adapter {name}: {e}")
        log.info("TaskService cleanup completed")

    def _shutdown_batch_pool(self) -> None:
        pool, self._batch_executor = self._batch_executor, None
        if pool is not None:
            # 停机后尚未开始的 detached 批任务没有调用方，直接取消可避免进程拖尾。
            pool.shutdown(wait=False, cancel_futures=True)

    def _drain_adapters(self) -> list[tuple[str, DownloaderAdapter]]:
        """原子清空缓存并返回需要关闭的适配器。"""
        with self._cache_lock:
            adapters = list(self._adapter_cache.items())
            self._adapter_cache.clear()
        return adapters


def _refresh_registry_after_install(job: Any) -> None:
    from ipclick.adapters import registry

    registry.refresh()
    log.info(f"远程组件任务结束（{getattr(job, 'title', '')}），已刷新适配器注册表")


def _version() -> str:
    try:
        from ipclick import __version__

        return __version__
    except Exception:
        return ""


def batch_metadata(context: object) -> tuple[tuple[str, str], ...]:
    """安全复制批处理上下文的 metadata，供每个 detached 子请求使用。"""
    getter = cast(Callable[[], Any] | None, getattr(context, "invocation_metadata", None))
    if not callable(getter):
        return ()
    try:
        metadata: Any = getter() or ()
        return tuple((str(k), str(v)) for k, v in metadata)
    except Exception:
        return ()
