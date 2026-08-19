from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
import json
import queue
import threading
import time
from typing import Any, cast

import grpc
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
from ipclick.exceptions import AdapterError, URLNotAllowedError
from ipclick.limiter import HostLimiter, HostLimitTimeout, build_limiter
from ipclick.trace import RequestTrace, TraceRecorder, get_recorder
from ipclick.utils import json_hook
from ipclick.utils.config_util import Settings
from ipclick.utils.log_util import log
from ipclick.utils.url_util import URLPolicy, validate_url


_DEFAULT_VERIFY_SSL = True
_DEFAULT_ALLOW_REDIRECTS = True
_DEFAULT_STREAM = False


class CallerGone(Exception):
    pass


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


class _IsolatedContext:
    def __init__(self, metadata: tuple[tuple[str, str], ...] = ()) -> None:
        self._metadata: tuple[tuple[str, str], ...] = metadata

    def set_code(self, _code: object) -> None: ...

    def set_details(self, _details: str) -> None: ...

    def is_active(self) -> bool:
        return True

    def invocation_metadata(self) -> tuple[tuple[str, str], ...]:
        return self._metadata


class TaskService(task_pb2_grpc.TaskServiceServicer):
    def __init__(self, config: Settings):
        self.config: Settings = config

        self.adapter_settings: AdapterSettings = AdapterSettings.from_config(dict(self.config.get("DOWNLOADER", {})))
        self.browser_settings: BrowserSettings = BrowserSettings.from_config(dict(self.config.get("BROWSER", {})))

        self._adapter_cache: dict[str, DownloaderAdapter] = {}
        self._cache_lock: threading.Lock = threading.Lock()
        self._recorder: TraceRecorder = get_recorder()
        self.node_id: str = str(dict(self.config.get("CLUSTER", {})).get("self_id", "") or "") or (
            self._recorder.node_id
        )

        downloader_config = dict(self.config.get("DOWNLOADER", {}))
        self._chunk_size: int = int(downloader_config.get("chunk_size", DEFAULT_CHUNK_SIZE) or DEFAULT_CHUNK_SIZE)
        self._batch_concurrency: int = max(1, int(dict(self.config.get("SERVER", {})).get("max_workers", 10) or 10))
        self._batch_executor: ThreadPoolExecutor | None = None
        self._started_at: float = time.monotonic()
        self._install_manager: Any = None

        self.url_policy: URLPolicy = URLPolicy.from_config(dict(self.config.get("SECURITY", {})))

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

    def limiters_for_sharding(self) -> list[Any]:
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
        log.debug("Received request: {} for URL: {}", request.uuid, request.url)
        start_time = time.monotonic()
        method_name = METHOD_MAP.get(request.method, "GET")

        try:
            adapter_name = IPClickAdapter.from_pb(request.adapter).display_name
        except ValueError:
            adapter_name = "unknown"

        with self._recorder.track_request(adapter_name, method_name, uuid=request.uuid, url=request.url) as tr:
            tr.node_id = self.node_id
            tr.forwarded = is_forwarded(context)
            try:
                adapter_member = IPClickAdapter.from_pb(request.adapter)
                adapter_name = adapter_member.display_name
                adapter = self._get_cached_adapter(adapter_name)
                tr.adapter = adapter.adapter_name

                validate_url(request.url, self.url_policy)

                if not caller_still_waiting(context):
                    log.info(f"Request {request.uuid} 在开工前发现调用方已断开，放弃")
                    raise CallerGone

                response = self._execute_download(adapter, request, tr)
                tr.attempts = response.attempts
                grpc_response = self._build_grpc_response(request, response, tr)

            except Exception as e:
                grpc_response = self._response_for_exception(e, request, tr, context)

            tr.status_code = grpc_response.status_code
            tr.size = len(grpc_response.content)
            tr.error = grpc_response.error_message

        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        grpc_response.response_time_ms = elapsed_ms

        log.debug(
            "Request {} completed in {}ms, status: {}, adapter: {}",
            request.uuid,
            elapsed_ms,
            grpc_response.status_code,
            adapter_name,
        )
        return grpc_response

    def _response_for_exception(
        self, exc: Exception, request: "task_pb2.ReqTask", tr: RequestTrace, context: ServicerContext
    ) -> "task_pb2.TaskResp":
        if isinstance(exc, CallerGone):
            tr.status_code = -1
            tr.error = "调用方已断开"
            return self._build_error_response(request, "调用方已断开，请求未执行", tr)

        if isinstance(exc, URLNotAllowedError):
            log.warning(f"Request {request.uuid} rejected: {exc}")
            self._recorder.record_rejected("url_not_allowed")
            context.set_code(grpc.StatusCode.PERMISSION_DENIED)
            context.set_details(str(exc))
            return self._build_error_response(request, str(exc), tr)

        if isinstance(exc, HostLimitTimeout):
            log.warning(f"Request {request.uuid} throttled: {exc}")
            self._recorder.record_rejected("host_limit")
            context.set_code(grpc.StatusCode.RESOURCE_EXHAUSTED)
            context.set_details(str(exc))
            return self._build_error_response(request, str(exc), tr)

        if isinstance(exc, AdapterError):
            log.warning(f"Request {request.uuid} cannot be served: {exc}")
            self._recorder.record_rejected("failed_precondition")
            context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
            context.set_details(str(exc))
            return self._build_error_response(request, str(exc), tr)

        if isinstance(exc, ValueError):
            log.warning(f"Request {request.uuid} invalid: {exc}")
            self._recorder.record_rejected("invalid_argument")
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(str(exc))
            return self._build_error_response(request, str(exc), tr)

        log.exception(f"Request {request.uuid} failed unexpectedly: {exc}")
        self._recorder.record_rejected("internal_error")
        return self._build_error_response(request, f"内部错误: {type(exc).__name__}", tr)

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
        log.debug("Received stream request: {} for URL: {}", request.uuid, request.url)
        start_time = time.monotonic()
        total_bytes = 0
        error_message = ""

        try:
            adapter_name = IPClickAdapter.from_pb(request.adapter).display_name
        except ValueError:
            adapter_name = "unknown"

        with self._recorder.track_request(
            adapter_name,
            METHOD_MAP.get(request.method, "GET"),
            uuid=request.uuid,
            url=request.url,
            stream=True,
        ) as tr:
            tr.node_id = self.node_id
            tr.forwarded = is_forwarded(context)
            try:
                adapter = self._get_cached_adapter(adapter_name)
                tr.adapter = adapter.adapter_name
                validate_url(request.url, self.url_policy)

                download_kwargs = self._build_download_kwargs(request)
                download_kwargs.pop("max_retries", None)
                download_kwargs.pop("retry_delay", None)

                stream = self._limited_stream(
                    request.url,
                    adapter.download_stream(request.url, chunk_size=self._chunk_size, **download_kwargs),
                )

                header_sent = False
                for event in stream:
                    if context.is_active() is False:
                        log.info(f"Stream request {request.uuid} cancelled by client")
                        break

                    if isinstance(event, StreamHeader):
                        tr.status_code = event.status_code
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
                self._recorder.record_rejected("url_not_allowed")
                context.set_code(grpc.StatusCode.PERMISSION_DENIED)
                context.set_details(error_message)
                yield self._stream_error_header(request, error_message)
            except HostLimitTimeout as e:
                error_message = str(e)
                self._recorder.record_rejected("host_limit")
                context.set_code(grpc.StatusCode.RESOURCE_EXHAUSTED)
                context.set_details(error_message)
                yield self._stream_error_header(request, error_message)
            except AdapterError as e:
                error_message = str(e)
                self._recorder.record_rejected("failed_precondition")
                context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
                context.set_details(error_message)
                yield self._stream_error_header(request, error_message)
            except ValueError as e:
                error_message = str(e)
                self._recorder.record_rejected("invalid_argument")
                context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
                context.set_details(error_message)
                yield self._stream_error_header(request, error_message)
            except Exception as e:
                log.exception(f"Stream request {request.uuid} failed: {e}")
                self._recorder.record_rejected("internal_error")
                error_message = f"内部错误: {type(e).__name__}"
                yield self._stream_error_header(request, error_message)

            tr.size = total_bytes
            tr.error = error_message
            if error_message and tr.status_code < 0:
                tr.status_code = -1

        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        yield task_pb2.TaskRespChunk(
            trailer=task_pb2.TaskRespTrailer(
                response_time_ms=elapsed_ms,
                total_bytes=total_bytes,
                error_message=error_message,
            )
        )
        log.debug("Stream request {} finished in {}ms, {} bytes", request.uuid, elapsed_ms, total_bytes)

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
        return self.Send(request, cast(ServicerContext, cast(object, _IsolatedContext(metadata))))

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
        return bool(dict(self.config.get("CLUSTER", {})).get("allow_remote_install", False))

    @override
    def Component(self, request: "task_pb2.ComponentReq", context: ServicerContext) -> "task_pb2.ComponentResp":
        if not self.remote_install_allowed:
            context.set_code(grpc.StatusCode.PERMISSION_DENIED)
            context.set_details(
                "本节点未开启远程组件管理。要允许主控代装，请在这台机器的配置里设置 "
                '[CLUSTER].allow_remote_install = true 并重启——它等于"能调本节点的人可以在本机跑 pip"，'
                "所以默认是关的。"
            )
            return task_pb2.ComponentResp(ok=False, message="远程组件管理未开启", node_id=self.node_id)

        op = (request.op or "").strip()
        if request.from_node:
            log.info(f"节点 {request.from_node} 请求在本机执行组件操作：{op} {request.extra}")

        if op == "list":
            return task_pb2.ComponentResp(
                ok=True,
                message="",
                node_id=self.node_id,
                components_json=self._components_json(),
                job=self._component_job(),
            )

        manager = self._installer()
        if op == "install":
            ok, message = manager.install(request.extra)
        elif op == "uninstall":
            ok, message = manager.uninstall(request.extra)
        elif op == "browser":
            ok, message = manager.fetch_browser(request.extra, request.browser_kind or "chromium")
        elif op == "status":
            ok, message = True, ""
        else:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(f"未知的组件操作 {op!r}（可选：list / status / install / uninstall / browser）")
            return task_pb2.ComponentResp(ok=False, message=f"未知的组件操作 {op!r}", node_id=self.node_id)

        return task_pb2.ComponentResp(
            ok=ok,
            message=message,
            node_id=self.node_id,
            components_json=self._components_json(),
            job=self._component_job(),
        )

    def _installer(self) -> Any:
        if self._install_manager is None:
            from ipclick.web.installer import InstallManager

            self._install_manager = InstallManager()
            self._install_manager.on_finished = _refresh_registry_after_install
        return self._install_manager

    def _components_json(self) -> str:
        import json as json_lib

        from ipclick.adapters.browser_settings import BrowserSettings
        from ipclick.components import snapshot

        browser = BrowserSettings.from_config(dict(self.config.get("BROWSER", {})))
        return json_lib.dumps(snapshot(browser), ensure_ascii=False, default=str)

    def _component_job(self) -> "task_pb2.ComponentJob | None":
        current = self._installer().current()
        if not current:
            return None
        progress = dict(current.get("progress") or {})
        percent = progress.get("percent")
        return task_pb2.ComponentJob(
            id=str(current.get("id", "")),
            title=str(current.get("title", "")),
            command=str(current.get("command", "")),
            status=str(current.get("status", "")),
            returncode=int(current.get("returncode") or 0),
            elapsed_seconds=int(current.get("elapsed") or 0),
            percent=float(percent) if percent is not None else -1.0,
            done_bytes=int(progress.get("done_bytes") or 0),
            speed_bytes=float(progress.get("speed") or 0.0),
            phase=str(progress.get("phase") or ""),
            output=list(current.get("output") or []),
        )

    @property
    def auth_required(self) -> bool:
        from ipclick.auth import load_tokens
        from ipclick.cluster.tokens import cluster_secret

        section = dict(self.config.get("SECURITY", {}))
        return bool(load_tokens(section)) or bool(cluster_secret(dict(self.config.get("CLUSTER", {}))))

    @property
    def forward_enabled(self) -> bool:
        return False

    def cleanup(self) -> None:
        log.info("Cleaning up TaskService resources...")

        pool, self._batch_executor = self._batch_executor, None
        if pool is not None:
            pool.shutdown(wait=False)

        with self._cache_lock:
            for name, adapter in self._adapter_cache.items():
                try:
                    adapter.close()
                    log.debug(f"Closed adapter: {name}")
                except Exception as e:
                    log.warning(f"Error closing adapter {name}: {e}")
            self._adapter_cache.clear()

        log.info("TaskService cleanup completed")


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
