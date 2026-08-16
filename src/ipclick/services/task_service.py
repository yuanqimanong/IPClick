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


# proto 未设置这些字段时服务端采用的默认值。
# 超时与重试三项改从 [DOWNLOADER] 配置读取（见 self.adapter_settings），
# 这里只保留没有对应配置项的布尔默认值。
_DEFAULT_VERIFY_SSL = True
_DEFAULT_ALLOW_REDIRECTS = True
_DEFAULT_STREAM = False


class _CallerGone(Exception):
    """调用方在我们开工前就断开了。内部信号，不外泄。"""


def _caller_still_waiting(context: object) -> bool:
    """调用方还在等吗。

    探测不出来时按"还在等"处理：批量路径和测试里传的都是假 context，
    误判成断开会让正常请求凭空失败。
    """
    checker = getattr(context, "is_active", None)
    if not callable(checker):
        return True
    try:
        return bool(checker())
    except Exception:  # pragma: no cover - 假 context 的兜底
        return True


#: 转发标记。集群里 A 把任务转给子节点时带上这个 metadata，子节点据此
#: 知道自己不是入口（用于链路展示，也是防转发环路的依据）。
FORWARD_HEADER = "ipclick-forwarded"


def is_forwarded(context: object) -> bool:
    """这次调用是别的节点转发过来的吗。

    用 getattr 探测而不是直接调：批量路径传进来的是 _IsolatedContext，
    测试里也常传各种假 context。
    """
    getter = cast(Callable[[], Any] | None, getattr(context, "invocation_metadata", None))
    if not callable(getter):
        return False
    try:
        metadata: Any = getter() or ()
        return any(str(key).lower() == FORWARD_HEADER for key, _value in metadata)
    except Exception:  # pragma: no cover - 假 context 的兜底
        return False


def _build_trace(tr: RequestTrace | None) -> "task_pb2.Trace | None":
    """把内部链路对象转成响应里的 Trace。

    返回 None 时 protobuf 会把这个字段留空（而不是塞一条全零的 Trace），
    调用方用 HasField("trace") 就能区分"服务端没记"和"记了但都是 0"。
    """
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
    """批量里给单个任务用的假 ServicerContext。

    只吞掉状态码设置，不影响批量共享的那条流；但要把真实的 metadata 透过来，
    否则批量任务的链路里会看不到转发标记。
    """

    def __init__(self, metadata: tuple[tuple[str, str], ...] = ()) -> None:
        self._metadata: tuple[tuple[str, str], ...] = metadata

    def set_code(self, _code: object) -> None: ...

    def set_details(self, _details: str) -> None: ...

    def is_active(self) -> bool:
        return True

    def invocation_metadata(self) -> tuple[tuple[str, str], ...]:
        return self._metadata


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
        self._recorder: TraceRecorder = get_recorder()
        # 本节点标识。刻意存在服务实例上而不是改记录器的字段——记录器是进程级
        # 单例，同一进程里起多个服务（测试、或一个进程内跑多实例）时改它会串台：
        # 后起的那个会把先起的那个的 node_id 覆盖掉。
        self.node_id: str = str(dict(self.config.get("CLUSTER", {})).get("self_id", "") or "") or (
            self._recorder.node_id
        )

        downloader_config = dict(self.config.get("DOWNLOADER", {}))
        self._chunk_size: int = int(downloader_config.get("chunk_size", DEFAULT_CHUNK_SIZE) or DEFAULT_CHUNK_SIZE)
        # 批量的并发度沿用 SERVER.max_workers：这里再开一个不受约束的池的话，
        # 总并发会变成 max_workers x 批量数，把下游打爆。
        self._batch_concurrency: int = max(1, int(dict(self.config.get("SERVER", {})).get("max_workers", 10) or 10))
        #: 批量任务共用的线程池。懒建，见 _batch_pool()。
        self._batch_executor: ThreadPoolExecutor | None = None
        #: 供 Ping 报告运行时长。用 monotonic：系统时钟被 NTP 调过之后
        #: wall clock 的差值会变成负数或者跳几个小时。
        self._started_at: float = time.monotonic()

        # 目标 URL 准入策略（SSRF 防护）
        self.url_policy: URLPolicy = URLPolicy.from_config(dict(self.config.get("SECURITY", {})))

        # 按 host 的并发与速率闸门。未配置时是零开销的空操作。
        self.host_limiter: HostLimiter = build_limiter(downloader_config)

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

    def _cache_key(self, name: str) -> str:
        """把请求里写的适配器名归一成缓存键。

        必须先把通用的 ``browser`` 解析成具体引擎名，否则 ``adapter="browser"``
        和 ``adapter="playwright"`` 会各建一个适配器实例——而浏览器适配器每个实例
        自带一个浏览器进程，于是同一个节点上跑着两个 chromium。集群里 3 个节点
        就是 6 个，在小内存机器上直接把自己挤爆。
        """
        if name == GENERIC_BROWSER_NAME:
            try:
                return resolve_browser_adapter_name(self.browser_settings)
            except Exception:
                # 引擎名配错了：保持原样，让 get_adapter 去抛那个更具体的错
                return name
        return name

    def _get_cached_adapter(self, name: str) -> DownloaderAdapter:
        """按名称取适配器，并缓存实例。

        原实现只读不写这个缓存，于是每个请求都要新建一次适配器（含
        ``UserAgent()`` 生成器），``cleanup()`` 也永远无事可做。
        """
        key = self._cache_key(name)
        adapter = self._adapter_cache.get(key)
        if adapter is not None:
            return adapter

        with self._cache_lock:
            if key not in self._adapter_cache:
                self._adapter_cache[key] = get_adapter(name, self.adapter_settings, self.browser_settings)
            return self._adapter_cache[key]

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
        # 固定了它，放在 try 里面赋值的话所有记录都会记成 "unknown"。
        # 枚举值非法时用 "unknown" 是正确的——下面的 from_pb 会抛错并记为拒绝。
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
                # 用适配器自己报的名字：adapter=BROWSER 时这里会是被解析出来的
                # 具体引擎（camoufox / drissionpage），比 "browser" 有用得多。
                tr.adapter = adapter.adapter_name

                validate_url(request.url, self.url_policy)

                # 调用方已经走了就别开工了。浏览器渲染一次能占几十秒和一个页面
                # 额度，用户关掉标签页之后还接着跑纯属浪费——尤其在小内存机器上，
                # 反复点几次「试一试」就能把浏览器额度和内存全占死。
                if not _caller_still_waiting(context):
                    log.info(f"Request {request.uuid} 在开工前发现调用方已断开，放弃")
                    raise _CallerGone

                response = self._execute_download(adapter, request, tr)
                tr.attempts = response.attempts
                grpc_response = self._build_grpc_response(request, response, tr)

            except _CallerGone:
                tr.status_code = -1
                tr.error = "调用方已断开"
                grpc_response = self._build_error_response(request, "调用方已断开，请求未执行", tr)
            except URLNotAllowedError as e:
                log.warning(f"Request {request.uuid} rejected: {e}")
                self._recorder.record_rejected("url_not_allowed")
                context.set_code(grpc.StatusCode.PERMISSION_DENIED)
                context.set_details(str(e))
                grpc_response = self._build_error_response(request, str(e), tr)
            except HostLimitTimeout as e:
                # 本机限流策略生效，不是目标站点或网络的问题。RESOURCE_EXHAUSTED
                # 是 gRPC 里表达"被限流了，稍后再来"的标准状态码。
                log.warning(f"Request {request.uuid} throttled: {e}")
                self._recorder.record_rejected("host_limit")
                context.set_code(grpc.StatusCode.RESOURCE_EXHAUSTED)
                context.set_details(str(e))
                grpc_response = self._build_error_response(request, str(e), tr)
            except AdapterError as e:
                # 与 ValueError 分开：这是"本服务端做不到"（适配器不存在、依赖没装、
                # 浏览器渲染被关掉），不是调用方参数写错。混成 INVALID_ARGUMENT
                # 会让调用方去改自己的参数，而实际要改的是服务端部署。
                log.warning(f"Request {request.uuid} cannot be served: {e}")
                self._recorder.record_rejected("failed_precondition")
                context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
                context.set_details(str(e))
                grpc_response = self._build_error_response(request, str(e), tr)
            except ValueError as e:
                log.warning(f"Request {request.uuid} invalid: {e}")
                self._recorder.record_rejected("invalid_argument")
                context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
                context.set_details(str(e))
                grpc_response = self._build_error_response(request, str(e), tr)
            except Exception as e:
                # 任何未预期的异常都不应该让 RPC 以 UNKNOWN + 堆栈的形式返回，
                # 调用方拿不到结构化信息，服务端也可能泄漏内部路径。
                log.exception(f"Request {request.uuid} failed unexpectedly: {e}")
                self._recorder.record_rejected("internal_error")
                grpc_response = self._build_error_response(request, f"内部错误: {type(e).__name__}", tr)

            tr.status_code = grpc_response.status_code
            tr.size = len(grpc_response.content)
            tr.error = grpc_response.error_message

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
    def _decode_body(raw: str | bytes, field_name: str) -> Any:
        """把线上的请求体还原成适配器能用的形式。

        ``data`` 在 proto 里是 bytes（``json`` 仍是 string）。还原顺序：
        JSON 对象 -> UTF-8 文本 -> 原始 bytes。最后那一档是关键——二进制体
        （图片、gzip）解不成文本，必须原样交给 HTTP 库，而不是报错或损坏。
        """
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
        """把 protobuf 请求翻译成适配器参数。

        单请求与流式两条路径共用，避免默认值处理（尤其是 proto3 显式存在性
        那套逻辑）在两处各写一份而失步。
        """
        method = METHOD_MAP.get(request.method, "GET")

        headers = dict(request.headers) if request.headers else None
        cookies = dict(request.cookies) if request.cookies else None

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
            "automation_config": request.automation_config or None,
            "automation_script": request.automation_script or None,
            "allowed_status_codes": list(request.allowed_status_codes) or None,
            "kwargs": request.kwargs or None,
        }

        # timeout 为 0 会让适配器立刻超时，这里兜底成默认值
        if not download_kwargs["timeout"] or download_kwargs["timeout"] <= 0:
            download_kwargs["timeout"] = self.adapter_settings.download_timeout

        return download_kwargs

    def _execute_download(
        self, adapter: DownloaderAdapter, request: task_pb2.ReqTask, tr: RequestTrace | None = None
    ) -> Response:
        """执行一次（非流式）下载。"""
        waiting_since = time.monotonic()
        with self.host_limiter.acquire(request.url):
            if tr is not None:
                # 在闸门里排的时间要和真正的下载耗时分开记：两者都是"慢"，
                # 但一个该调限流配置，另一个该查目标站点或网络。
                tr.queued_ms = int((time.monotonic() - waiting_since) * 1000)
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
    def _build_grpc_response(
        request: task_pb2.ReqTask, response: Response, tr: RequestTrace | None = None
    ) -> task_pb2.TaskResp:
        """
        构建gRPC响应

        Args:
            request: 原始gRPC请求
            response: 统一响应对象
            tr: 链路信息，会作为 TaskResp.trace 一起返回

        Returns:
            task_pb2.TaskResp: gRPC响应对象
        """
        return task_pb2.TaskResp(
            request_uuid=request.uuid,
            adapter=request.adapter,
            # 不回传 original_request：它含代理账号密码等凭证，且会让每个响应
            # 白白多带一份完整请求体。要查链路请看 trace。
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
        """构建一个表示失败的响应（状态码 -1，与适配器侧保持一致）。"""
        return task_pb2.TaskResp(
            request_uuid=request.uuid,
            adapter=request.adapter,
            effective_url=request.url,
            status_code=-1,
            error_message=message,
            trace=_build_trace(tr),
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
        pool = self._batch_pool()
        # 完成通知走队列而不是每提交一个就扫一遍 pending。
        #
        # 旧写法是 `done = [f for f in pending if f.done()]`，放在提交循环里——
        # 每提交一个任务就把**全部**在途 future 扫一遍，总代价 O(N²)。批量一千个
        # 任务就是五十万次 done() 调用，全压在推任务的那个线程上，反而拖慢了提交
        # 本身。add_done_callback 让完成方自己来报到，两边都是 O(1)。
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

            # 边收边发：不等客户端把所有任务推完再开始返回结果，
            # 否则一个超长的批次会让首个结果迟迟不到。
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
        """批量任务共用的线程池，懒建一次。

        旧实现每次 ``SendBatch`` 都 ``with ThreadPoolExecutor(...)``，于是每一次
        批量调用都要重新创建、再销毁最多 ``[SERVER].max_workers`` 个线程（默认
        100）。批量本来就是"高频、每次很多任务"的用法，这份线程创建开销是白付的。
        共用一个池之后，线程只在第一次真正需要时创建，之后一直复用。

        并发度仍然受 ``[SERVER].max_workers`` 约束——这里再开一个不受约束的池的话，
        总并发会变成 max_workers × 并发批量数，把下游打爆。
        """
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
        """批量里的单个任务，复用 Send 的全部逻辑（含链路记录与安全校验）。

        刻意传一个隔离的假 context：Send 在出错时会调 set_code/set_details，
        若把批量共享的那个 context 传进去，一个任务的 URL 被 SSRF 拦截就会把
        **整条批量流**标记成 PERMISSION_DENIED，其余任务的结果全部丢失。
        每个任务的失败信息已经在它自己的 TaskResp.error_message 里了。
        """
        # _IsolatedContext 只实现了 Send 实际用到的三个方法，
        # 不是完整的 ServicerContext，这里显式 cast。
        return self.Send(request, cast(ServicerContext, cast(object, _IsolatedContext(metadata))))

    # ------------------------------------------------------------------ #
    # 探测
    # ------------------------------------------------------------------ #

    @override
    def Ping(self, request: "task_pb2.PingReq", context: ServicerContext) -> "task_pb2.PingResp":
        """节点探测：验连通性与鉴权，不做任何业务动作。

        能走到这个方法就说明鉴权拦截器已经放行了——这正是它相对
        ``grpc.health.v1`` 的全部价值：健康检查刻意免鉴权，于是"连不上"和
        "连上了但令牌不对"在它眼里长得一样，而这两件事的排查方向完全相反。

        刻意不落链路记录：这是诊断动作，不是业务请求，混进请求流只会污染统计。
        """
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
    def auth_required(self) -> bool:
        """本节点有没有启用令牌鉴权。

        对端要能看到这一位：一个**没设防**的节点意味着任何能连到它端口的人都能
        借它发请求，而探测成功本身并不能区分"我的令牌对"和"它根本不验"。
        """
        from ipclick.auth import load_tokens
        from ipclick.cluster.tokens import cluster_secret

        section = dict(self.config.get("SECURITY", {}))
        return bool(load_tokens(section)) or bool(cluster_secret(dict(self.config.get("CLUSTER", {}))))

    @property
    def forward_enabled(self) -> bool:
        """本节点是否开着服务端转发。基类恒为 False，转发子类覆盖。"""
        return False

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def cleanup(self) -> None:
        """
        清理资源

        关闭所有适配器连接，释放资源
        """
        log.info("Cleaning up TaskService resources...")

        # 批量线程池是共用的，进程收尾时要显式关掉——否则那些线程会一直挂着
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

        # 注意：不要清空 registry.ADAPTER_CLASSES。那是模块级的类型注册表，
        # 清掉之后同一进程内再也造不出任何适配器（例如测试里起第二个服务）。
        log.info("TaskService cleanup completed")


def _version() -> str:
    """本进程的 IPClick 版本。取不到时报空串而不是抛错——Ping 的价值在于
    "连得上、鉴权过"，版本号只是顺带的信息，不该因为它让探测整个失败。
    """
    try:
        from ipclick import __version__

        return __version__
    except Exception:  # pragma: no cover
        return ""


def _batch_metadata(context: object) -> tuple[tuple[str, str], ...]:
    """取批量流的 metadata，供每个子任务的隔离 context 复用。"""
    getter = cast(Callable[[], Any] | None, getattr(context, "invocation_metadata", None))
    if not callable(getter):
        return ()
    try:
        metadata: Any = getter() or ()
        return tuple((str(k), str(v)) for k, v in metadata)
    except Exception:  # pragma: no cover
        return ()
