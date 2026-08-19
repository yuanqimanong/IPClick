from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
import json
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
from ipclick.adapters.settings import AdapterSettings
from ipclick.dto.models import METHOD_MAP, IPClickAdapter
from ipclick.dto.proto import task_pb2, task_pb2_grpc
from ipclick.dto.response import Response
from ipclick.limiter import HostLimiter, build_limiter
from ipclick.protocols import ShardableLimiter
from ipclick.server_settings import ServerSettings
from ipclick.services.components import ComponentService
from ipclick.services.detached import DetachedContext
from ipclick.services.errors import CallerGone, classify, report
from ipclick.trace import RequestTrace, TraceRecorder, get_recorder
from ipclick.utils import json_hook
from ipclick.utils.config_util import Settings, section
from ipclick.utils.log_util import log
from ipclick.utils.url_util import URLPolicy, validate_url


_DEFAULT_VERIFY_SSL = True
_DEFAULT_ALLOW_REDIRECTS = True
_DEFAULT_STREAM = False


def caller_still_waiting(context: object) -> bool:
    checker = getattr(context, "is_active", None)
    if not callable(checker):
        return True
    try:
        return bool(checker())
    except Exception:
        return True


FORWARD_HEADER = "ipclick-forwarded"


def is_forwarded(context: object) -> bool:
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
    def __init__(self, config: Settings):
        self.config: Settings = config

        self.adapter_settings: AdapterSettings = AdapterSettings.from_config(section(self.config, "DOWNLOADER"))
        self.browser_settings: BrowserSettings = BrowserSettings.from_config(section(self.config, "BROWSER"))

        self._adapter_cache: dict[str, DownloaderAdapter] = {}
        self._cache_lock: threading.Lock = threading.Lock()
        self._recorder: TraceRecorder = get_recorder()
        self.node_id: str = str(section(self.config, "CLUSTER").get("self_id", "") or "") or (self._recorder.node_id)

        downloader_config = section(self.config, "DOWNLOADER")
        self._chunk_size: int = int(downloader_config.get("chunk_size", DEFAULT_CHUNK_SIZE) or DEFAULT_CHUNK_SIZE)
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
        return [self.host_limiter]

    def _cache_key(self, name: str) -> str:
        if name == GENERIC_BROWSER_NAME:
            try:
                return resolve_browser_adapter_name(self.browser_settings)
            except Exception:
                return name
        return name

    def _get_cached_adapter(self, name: str) -> DownloaderAdapter:
        key = self._cache_key(name)
        adapter = self._adapter_cache.get(key)
        if adapter is not None:
            return adapter

        with self._cache_lock:
            if key not in self._adapter_cache:
                self._adapter_cache[key] = get_adapter(name, self.adapter_settings, self.browser_settings)
            return self._adapter_cache[key]

    @override
    def Send(self, request: "task_pb2.ReqTask", context: ServicerContext) -> "task_pb2.TaskResp":
        started = time.monotonic()
        with self.track(request, context) as tr:
            try:
                adapter = self.prepare(request, context, tr)
                response = self._execute_download(adapter, request, tr)
                grpc_response = self.accept(request, response, tr)
            except Exception as e:
                grpc_response = self._response_for_exception(e, request, tr, context)
            self.record_outcome(tr, grpc_response)
        return self.stamp_elapsed(grpc_response, started, request)

    @contextmanager
    def track(self, request: "task_pb2.ReqTask", context: ServicerContext, *, stream: bool = False):
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
        adapter = self._get_cached_adapter(_adapter_display_name(request.adapter))
        tr.adapter = adapter.adapter_name
        validate_url(request.url, self.url_policy)
        if not caller_still_waiting(context):
            raise CallerGone
        return adapter

    def accept(self, request: "task_pb2.ReqTask", response: Response, tr: RequestTrace) -> "task_pb2.TaskResp":
        tr.attempts = response.attempts
        return self._build_grpc_response(request, response, tr)

    @staticmethod
    def record_outcome(tr: RequestTrace, grpc_response: "task_pb2.TaskResp") -> None:
        tr.status_code = grpc_response.status_code
        tr.size = len(grpc_response.content)
        tr.error = grpc_response.error_message

    @staticmethod
    def stamp_elapsed(
        grpc_response: "task_pb2.TaskResp", started: float, request: "task_pb2.ReqTask"
    ) -> "task_pb2.TaskResp":
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
        method = METHOD_MAP.get(request.method, "GET")

        headers = dict(request.headers) if request.headers else None
        cookies = dict(request.cookies) if request.cookies else None

        params = json.loads(request.params, object_hook=json_hook) if request.params else None
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
        self, adapter: DownloaderAdapter, request: task_pb2.ReqTask, tr: RequestTrace | None = None
    ) -> Response:
        waiting_since = time.monotonic()
        with self.host_limiter.acquire(request.url):
            if tr is not None:
                tr.queued_ms = int((time.monotonic() - waiting_since) * 1000)
            return adapter.download(request.url, **self._build_download_kwargs(request))

    def _limited_stream(self, url: str, stream: Iterator[StreamEvent]) -> Iterator[StreamEvent]:
        with self.host_limiter.acquire(url):
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
        started = time.monotonic()
        total_bytes = 0
        error_message = ""

        with self.track(request, context, stream=True) as tr:
            try:
                adapter = self.prepare(request, context, tr)
                header_sent = False
                for event in self._open_stream(adapter, request):
                    if context.is_active() is False:
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
        download_kwargs = self._build_download_kwargs(request)
        for retry_key in ("max_retries", "retry_delay"):
            _ = download_kwargs.pop(retry_key, None)
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
        pool = self._batch_pool()
        finished: queue.Queue[Future[task_pb2.TaskResp]] = queue.Queue()
        in_flight = 0
        submitted = 0

        for request in request_iterator:
            if not context.is_active():
                log.info("Batch cancelled by client")
                break
            future = pool.submit(self._handle_one, request, _batch_metadata(context))
            future.add_done_callback(finished.put)
            in_flight += 1
            submitted += 1

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

        log.info(f"Batch finished, {submitted} tasks")

    def _batch_pool(self) -> ThreadPoolExecutor:
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
        return self.components.enabled

    @override
    def Component(self, request: "task_pb2.ComponentReq", context: ServicerContext) -> "task_pb2.ComponentResp":
        return self.components.handle(request, context, node_id=self.node_id)

    @property
    def auth_required(self) -> bool:
        from ipclick.auth import load_tokens
        from ipclick.cluster.tokens import cluster_secret

        security = section(self.config, "SECURITY")
        return bool(load_tokens(security)) or bool(cluster_secret(section(self.config, "CLUSTER")))

    @property
    def forward_enabled(self) -> bool:
        return False

    def cleanup(self) -> None:
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
            pool.shutdown(wait=False)

    def _drain_adapters(self) -> list[tuple[str, DownloaderAdapter]]:
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


def _batch_metadata(context: object) -> tuple[tuple[str, str], ...]:
    getter = cast(Callable[[], Any] | None, getattr(context, "invocation_metadata", None))
    if not callable(getter):
        return ()
    try:
        metadata: Any = getter() or ()
        return tuple((str(k), str(v)) for k, v in metadata)
    except Exception:
        return ()
