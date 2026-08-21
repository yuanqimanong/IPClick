from __future__ import annotations

import json
from typing import Any, cast, final

import grpc
from grpc import ServicerContext
import pytest
from typing_extensions import override

from ipclick.adapters import registry
from ipclick.async_limiter import AsyncHostLimiter
from ipclick.cluster.async_forwarder import AsyncForwardingTaskService
from ipclick.cluster.forwarder import ForwardingTaskService
from ipclick.cluster.node import ClusterConfig, NodeState
from ipclick.cluster.pool import NodePool
from ipclick.dto.proto import task_pb2
from ipclick.exceptions import AdapterError, HostResolutionError, URLNotAllowedError, ValidationError
from ipclick.limiter import HostLimitTimeout, build_limiter
from ipclick.server import IPClickServer
from ipclick.services.async_task_service import AsyncTaskService
from ipclick.services.errors import CALLER_GONE_MESSAGE, CallerGone, classify
from ipclick.services.task_service import TaskService
from ipclick.utils.config_util import Settings

from .helpers import RecordingContext, StubAdapter


@pytest.fixture(autouse=True)
def stub_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(registry.ADAPTER_CLASSES, StubAdapter.adapter_name, StubAdapter)


def _cluster_settings() -> Settings:
    return Settings(
        {
            "SERVER": {"host": "127.0.0.1", "port": 19528, "max_workers": 2},
            "SECURITY": {},
            "DOWNLOADER": {},
            "BROWSER": {"enabled": False},
            "CLUSTER": {"forward": "on", "nodes": [{"id": "n1", "address": "127.0.0.1:19601"}]},
            "TRACE": {"sqlite_enabled": False},
        }
    )


def _gateway_settings() -> Settings:
    return Settings(
        {
            "SERVER": {"host": "127.0.0.1", "port": 19528, "max_workers": 2},
            "SECURITY": {},
            "DOWNLOADER": {},
            "BROWSER": {"enabled": False},
            "CLUSTER": {
                "forward": "on",
                "self_id": "gateway",
                "max_failover": 1,
                "nodes": [
                    {"id": "n1", "address": "127.0.0.1:19601"},
                    {"id": "n2", "address": "127.0.0.1:19602"},
                ],
            },
            "TRACE": {"sqlite_enabled": False},
        }
    )


class _AmbiguousNodeFault(grpc.RpcError):
    @override
    def code(self) -> grpc.StatusCode:
        return grpc.StatusCode.DEADLINE_EXCEEDED

    @override
    def details(self) -> str:
        return "response deadline expired"


def test_async_service_owns_its_limiter(settings: Settings) -> None:
    service = AsyncTaskService(settings)
    try:
        assert isinstance(service.async_limiter, AsyncHostLimiter)
        assert service.limiters_for_sharding() == [service.async_limiter]
    finally:
        service.cleanup()


def test_async_forwarding_service_initialises_both_branches() -> None:
    config = _cluster_settings()
    cluster = ClusterConfig.from_config(config["CLUSTER"])
    pool = NodePool(cluster, start_probing=False)
    service = AsyncForwardingTaskService(config, cluster, pool=pool, server_host="127.0.0.1", server_port=19601)
    try:
        assert isinstance(service.async_limiter, AsyncHostLimiter)
        assert service.self_id == "n1"
        assert service.forward_enabled is True
        assert service.host_limiter is not None
    finally:
        service.cleanup()
        pool.stop()


def test_forwarding_service_reports_itself_as_forwarding() -> None:
    config = _cluster_settings()
    cluster = ClusterConfig.from_config(config["CLUSTER"])
    pool = NodePool(cluster, start_probing=False)
    service = ForwardingTaskService(config, cluster, pool=pool, server_host="127.0.0.1", server_port=19601)
    try:
        assert service.forward_enabled is True
        assert service.node_id == "n1"
    finally:
        service.cleanup()
        pool.stop()


@pytest.mark.parametrize(
    "method",
    [task_pb2.POST, task_pb2.PUT, task_pb2.PATCH, task_pb2.DELETE],
)
def test_sync_forwarder_does_not_repeat_non_idempotent_requests(method: int, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _gateway_settings()
    cluster = ClusterConfig.from_config(config["CLUSTER"])
    pool = NodePool(cluster, start_probing=False)
    service = ForwardingTaskService(config, cluster, pool=pool, server_host="127.0.0.1", server_port=19528)
    calls: list[str] = []

    def fail(state: NodeState, _request: task_pb2.ReqTask) -> task_pb2.TaskResp:
        calls.append(state.node.id)
        raise _AmbiguousNodeFault

    monkeypatch.setattr(service, "_forward", fail)
    recording = RecordingContext()
    try:
        response = service.Send(
            task_pb2.ReqTask(uuid="unsafe", method=cast(Any, method), url="https://example.com/action"),
            cast(ServicerContext, cast(object, recording)),
        )
        assert len(calls) == 1
        assert recording.code is grpc.StatusCode.DEADLINE_EXCEEDED
        assert response.status_code == -1
    finally:
        service.cleanup()


def test_sync_forwarder_keeps_failover_for_safe_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _gateway_settings()
    cluster = ClusterConfig.from_config(config["CLUSTER"])
    pool = NodePool(cluster, start_probing=False)
    service = ForwardingTaskService(config, cluster, pool=pool, server_host="127.0.0.1", server_port=19528)
    calls: list[str] = []

    def forward(state: NodeState, request: task_pb2.ReqTask) -> task_pb2.TaskResp:
        calls.append(state.node.id)
        if len(calls) == 1:
            raise _AmbiguousNodeFault
        return task_pb2.TaskResp(request_uuid=request.uuid, status_code=200)

    monkeypatch.setattr(service, "_forward", forward)
    try:
        response = service.Send(
            task_pb2.ReqTask(uuid="safe", method=task_pb2.GET, url="https://example.com/read"),
            cast(ServicerContext, cast(object, RecordingContext())),
        )
        assert len(calls) == 2
        assert response.status_code == 200
    finally:
        service.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    [task_pb2.POST, task_pb2.PUT, task_pb2.PATCH, task_pb2.DELETE],
)
async def test_async_forwarder_does_not_repeat_non_idempotent_requests(
    method: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _gateway_settings()
    cluster = ClusterConfig.from_config(config["CLUSTER"])
    pool = NodePool(cluster, start_probing=False)
    service = AsyncForwardingTaskService(config, cluster, pool=pool, server_host="127.0.0.1", server_port=19528)
    calls: list[str] = []

    def fail(state: NodeState, _request: task_pb2.ReqTask) -> task_pb2.TaskResp:
        calls.append(state.node.id)
        raise _AmbiguousNodeFault

    monkeypatch.setattr(service, "_forward", fail)
    recording = RecordingContext()
    try:
        response = await service.Send(
            task_pb2.ReqTask(uuid="unsafe-async", method=cast(Any, method), url="https://example.com/action"),
            cast(ServicerContext, cast(object, recording)),
        )
        assert len(calls) == 1
        assert recording.code is grpc.StatusCode.DEADLINE_EXCEEDED
        assert response.status_code == -1
    finally:
        await service.acleanup()


@pytest.mark.asyncio
async def test_async_forwarder_keeps_failover_for_safe_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _gateway_settings()
    cluster = ClusterConfig.from_config(config["CLUSTER"])
    pool = NodePool(cluster, start_probing=False)
    service = AsyncForwardingTaskService(config, cluster, pool=pool, server_host="127.0.0.1", server_port=19528)
    calls: list[str] = []

    def forward(state: NodeState, request: task_pb2.ReqTask) -> task_pb2.TaskResp:
        calls.append(state.node.id)
        if len(calls) == 1:
            raise _AmbiguousNodeFault
        return task_pb2.TaskResp(request_uuid=request.uuid, status_code=200)

    monkeypatch.setattr(service, "_forward", forward)
    try:
        response = await service.Send(
            task_pb2.ReqTask(uuid="safe-async", method=task_pb2.GET, url="https://example.com/read"),
            cast(ServicerContext, cast(object, RecordingContext())),
        )
        assert len(calls) == 2
        assert response.status_code == 200
    finally:
        await service.acleanup()


def test_plain_service_is_not_forwarding(settings: Settings) -> None:
    service = TaskService(settings)
    try:
        assert service.forward_enabled is False
    finally:
        service.cleanup()


@pytest.mark.parametrize(
    ("error", "code", "label"),
    [
        (CallerGone(), None, ""),
        (URLNotAllowedError("blocked"), grpc.StatusCode.PERMISSION_DENIED, "url_not_allowed"),
        (HostLimitTimeout("slow down"), grpc.StatusCode.RESOURCE_EXHAUSTED, "host_limit"),
        (AdapterError("missing"), grpc.StatusCode.FAILED_PRECONDITION, "failed_precondition"),
        (ValidationError("bad"), grpc.StatusCode.INVALID_ARGUMENT, "invalid_argument"),
        (ValueError("bad"), grpc.StatusCode.INVALID_ARGUMENT, "invalid_argument"),
        (RuntimeError("boom"), None, "internal_error"),
    ],
)
def test_failures_are_classified_once_for_every_rpc(error: Exception, code: grpc.StatusCode | None, label: str) -> None:
    failure = classify(error)
    assert failure.code is code
    assert failure.label == label


def test_url_policy_rejection_beats_the_plain_value_error_rule() -> None:
    assert classify(URLNotAllowedError("x")).code is grpc.StatusCode.PERMISSION_DENIED


def test_caller_gone_carries_its_own_message() -> None:
    assert classify(CallerGone()).message == CALLER_GONE_MESSAGE


def test_internal_errors_do_not_leak_their_text() -> None:
    failure = classify(RuntimeError("db password is hunter2"))
    assert "hunter2" not in failure.message
    assert failure.level == "exception"


class _FakePool:
    """记录回调挂载的假节点池。"""

    def __init__(self) -> None:
        self.callbacks: list[object] = []
        self.stopped: bool = False

    def on_health_change(self, callback: object) -> None:
        self.callbacks.append(callback)

    def stop(self) -> None:
        self.stopped = True

    def drain(self, node_id: str) -> None:
        _ = node_id

    def __len__(self) -> int:
        return 2


def test_start_node_pool_is_idempotent() -> None:
    """第二次调用不得覆盖已有节点池。

    _wire_rate_sharding() 与 _start_web() 都会调 _start_node_pool()。不幂等的话第二次
    会顶掉第一个池，而第一个池的探活线程还在跑——每个节点被探两遍，限流分片的回调留在
    被遗弃的池上，而 stop() 只停得掉后一个。
    """
    server: Any = object.__new__(IPClickServer)
    existing = _FakePool()
    server._node_pool = existing

    server._start_node_pool()

    assert server._node_pool is existing


def test_hot_reload_reattaches_rate_sharding() -> None:
    """热更新重建节点池后必须重挂 set_cluster_size。

    不重挂的话限流器的存活节点数会永久冻结在重建前那一刻：加了节点之后每台仍按旧的
    （更大的）份额发，集群总 QPS 超配，且没有任何报错。
    """
    limiter = build_limiter({"rate_limit": {"per_host_qps": 10}})
    service: Any = object.__new__(TaskService)
    service.host_limiter = limiter

    server: Any = object.__new__(IPClickServer)
    server.task_service = service
    server.cluster_config = ClusterConfig.from_config(
        {"forward": "off", "nodes": [{"id": "n1", "address": "127.0.0.1:9528"}]}
    )
    pool = _FakePool()
    server._node_pool = pool

    assert server._attach_rate_sharding() is True
    assert limiter.set_cluster_size in pool.callbacks


def _forwarding_service(policy_blocks_private: bool) -> Any:
    """构造一个只装了准入策略与转发开关的转发服务。"""
    import threading

    from ipclick.trace import get_recorder
    from ipclick.utils.url_util import URLPolicy

    service: Any = object.__new__(ForwardingTaskService)
    service.cluster = ClusterConfig.from_config({"forward": "on", "nodes": [{"id": "n1", "address": "10.0.0.9:9528"}]})
    service.url_policy = URLPolicy(block_private_networks=policy_blocks_private)
    service._recorder = get_recorder()
    service._local_count = 0
    service._reload_lock = threading.Lock()
    service.node_id = "gw"
    return service


def test_entry_node_url_policy_applies_to_forwarded_requests() -> None:
    """开了服务端转发后，入口节点的 SSRF 准入必须对被转发的请求也生效。

    本地执行时准入在 prepare() 里，但被转发的请求走不到那里——于是"入口开了
    block_private_networks、工作节点用默认配置"这个常见组合下，整套策略对所有转发
    流量完全不生效。
    """
    service = _forwarding_service(policy_blocks_private=True)
    request = task_pb2.ReqTask(uuid="u1", url="http://10.0.0.9:8080/admin/dump", method=task_pb2.GET)
    context = RecordingContext()

    rejected = service._reject_if_url_not_allowed(request, cast(ServicerContext, cast(object, context)))

    assert rejected is not None
    assert rejected.status_code == -1
    assert "内网" in rejected.error_message


def test_entry_node_lets_allowed_urls_through_to_forwarding() -> None:
    """策略允许的 URL 不得被这道新增的门禁误伤。"""
    service = _forwarding_service(policy_blocks_private=False)
    request = task_pb2.ReqTask(uuid="u2", url="http://10.0.0.9:8080/ok", method=task_pb2.GET)

    assert service._reject_if_url_not_allowed(request, cast(ServicerContext, cast(object, RecordingContext()))) is None


def test_async_streaming_and_unary_share_one_quota_pool() -> None:
    """async 模式下流式与 unary 必须用同一个限流器，不能各占一份配额。

    AsyncTaskService 继承 TaskService 时也建了同步 host_limiter；如果 _limited_stream
    仍用它，就是两个独立配额池：unary 走 async_limiter、流式走 host_limiter，
    单 host 的有效并发直接翻倍，而且 limiters_for_sharding() 只上报 async_limiter，
    同步那份永远拿不到集群限流分片。
    """
    service: Any = object.__new__(AsyncTaskService)
    acquired: list[str] = []

    class _TrackingLimiter:
        def acquire(self, url: str, _timeout: float | None = None) -> Any:
            acquired.append(url)
            raise AssertionError("异步服务的流式路径不该走同步限流器")

    service.host_limiter = _TrackingLimiter()

    def stream() -> Any:
        yield b"chunk"

    events = list(service._limited_stream("https://example.com/f", stream()))

    assert events == [b"chunk"]
    assert acquired == []


def test_async_limited_stream_still_closes_the_underlying_stream() -> None:
    """不限流了，但必须仍然保证关掉底层 HTTP 流。"""
    service: Any = object.__new__(AsyncTaskService)
    closed: list[bool] = []

    class _Stream:
        def __iter__(self) -> Any:
            yield b"a"

        def close(self) -> None:
            closed.append(True)

    _ = list(service._limited_stream("https://example.com/f", _Stream()))
    assert closed == [True]


@pytest.mark.skipif(not hasattr(__import__("os"), "fork"), reason="多进程模式仅 Unix")
def test_fork_failure_reaps_already_started_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """中途 fork 失败时，已启动的 worker 必须被收掉，不能留成孤儿。

    留下的孤儿靠 SO_REUSEPORT 继续占着端口对外服务，表现是"启动报错了，但端口还通、
    改了配置也没反应"——极难定位。
    """
    import ipclick.multiprocess as mp
    from ipclick.server_settings import ServerSettings

    spawned: list[int] = []
    killed: list[int] = []

    def fake_spawn(index: int, _worker: Any) -> int:
        if index == 2:
            raise OSError("fork 失败：Resource temporarily unavailable")
        pid = 1000 + index
        spawned.append(pid)
        return pid

    monkeypatch.setattr(mp, "_spawn", fake_spawn)
    monkeypatch.setattr(mp, "probe_port", lambda host, port: None)
    monkeypatch.setattr(mp, "_shutdown_children", lambda children: killed.extend(children))

    with pytest.raises(OSError, match="fork 失败"):
        mp.run_workers(4, ServerSettings(), lambda index: None)

    # 前两个已启动的 worker 都被交给了收尾逻辑
    assert killed == spawned == [1000, 1001]


def test_query_params_are_not_rewritten_into_datetimes() -> None:
    """查询参数不得被 json_hook 还原成 datetime 再 str() 拼进 URL。

    params={"start": "2024-01-01"} 曾经会变成 start=2024-01-01 00:00:00 发出去，
    目标 API 的日期过滤直接失效或返回 400，而调用方从自己传的值里看不出问题。
    Python 3.11 起 fromisoformat 更宽松，"20241231" 这种紧凑写法也会被吃掉。
    """
    service: Any = object.__new__(TaskService)
    service.adapter_settings = None

    request = task_pb2.ReqTask(
        uuid="u",
        url="https://api.example.com/orders",
        method=task_pb2.GET,
        params='{"start": "2024-01-01", "end": "20241231", "q": "hello"}',
    )
    params = json.loads(request.params)
    assert params == {"start": "2024-01-01", "end": "20241231", "q": "hello"}
    assert all(isinstance(v, str) for v in params.values())


def test_streaming_honours_the_callers_retry_count() -> None:
    """流式路径不得静默忽略调用方的 max_retries。

    这条钉住一个来回踩过两次的地方：原实现把 max_retries / retry_delay 从 kwargs 里
    抹掉（→ 回落到服务端默认 3），后来改成写死 0（→ 完全不重试）。两种都是"参数传了
    没生效"，而写死 0 还额外抹掉了本来合理的重试。

    抹参数的理由曾写作"避免已发送内容被重复"，但它对现有两条路径都不成立：真流式的
    适配器没有被 @retry 装饰、根本不看这个值；其余适配器走基类兜底实现（先整体下载完
    再切块），download() 在第一个分片被 yield 之前就跑完了。
    """
    from ipclick.adapters.base import DownloaderAdapter, StreamHeader
    from ipclick.adapters.retry import retry
    from ipclick.adapters.settings import AdapterSettings
    from ipclick.dto.response import Response
    from ipclick.exceptions import TransportError

    calls: list[int] = []

    @final
    class _BaseFallbackAdapter(DownloaderAdapter):
        """不重写 download_stream —— 走基类兜底实现，和浏览器系一样。"""

        adapter_name: str = "test-fallback"

        @retry()
        def download(self, _url: str, **_kwargs: Any) -> Response:
            calls.append(1)
            raise TransportError("transient")

    adapter = _BaseFallbackAdapter(AdapterSettings())

    # 重试耗尽后 @retry 返回错误响应而不是抛异常，所以这里断言尝试次数。
    # 调用方要求 5 次重试 → 共 6 次尝试
    calls.clear()
    events = list(adapter.download_stream("https://example.com/f", max_retries=5, retry_delay=0))
    assert len(calls) == 6
    # 全部重试都发生在第一个分片被 yield 之前，所以不存在"重复已发送内容"
    assert isinstance(events[0], StreamHeader) and events[0].status_code == -1

    # 调用方要求不重试 → 只 1 次
    calls.clear()
    _ = list(adapter.download_stream("https://example.com/f", max_retries=0, retry_delay=0))
    assert len(calls) == 1


def test_open_stream_passes_retry_settings_through() -> None:
    """_open_stream 不得再删掉重试相关的 kwargs。"""
    service: Any = object.__new__(TaskService)
    service.adapter_settings = None
    captured: dict[str, Any] = {}

    @final
    class _Recording:
        adapter_name: str = "rec"

        def download_stream(self, url: str, **kwargs: Any) -> Any:
            captured.update(kwargs)
            _ = url
            return iter(())

    service._build_download_kwargs = lambda request: {"max_retries": 7, "retry_delay": 1.5, "method": "GET"}
    service._chunk_size = 1024
    service._limited_stream = lambda url, stream: stream
    _ = service._open_stream(cast(Any, _Recording()), task_pb2.ReqTask(url="https://example.com/f"))

    assert captured["max_retries"] == 7
    assert captured["retry_delay"] == 1.5


def test_dns_failure_becomes_an_ordinary_failed_response_not_a_grpc_error() -> None:
    """DNS 解析失败是网络故障，不该被还原成"被策略拒绝"的异常抛给调用方。

    code=None 意味着不设 gRPC 错误状态，于是它变成一条普通的失败响应
    （status_code == -1、error 非空），与"连不上目标站点"表现一致，
    也与 README 承诺的"不会因为网络问题抛异常"一致。
    真正的策略拒绝仍然是 PERMISSION_DENIED，两者的排查方向完全相反。
    """
    dns = classify(HostResolutionError("无法解析主机 'nope.invalid'"))
    policy = classify(URLNotAllowedError("禁止访问云元数据地址"))

    assert dns.code is None
    assert dns.label == "dns_failure"
    assert policy.code is grpc.StatusCode.PERMISSION_DENIED
