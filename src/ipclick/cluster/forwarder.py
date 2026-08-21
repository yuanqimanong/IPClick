"""服务端集群派发、故障转移和下游连接复用。"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import threading
from typing import Any, final

import grpc
from grpc import ServicerContext
from typing_extensions import override

from ipclick.auth import build_client_metadata
from ipclick.cluster.node import ClusterConfig, NodeState
from ipclick.cluster.pool import NodePool
from ipclick.cluster.tokens import cluster_secret, token_for, warn_if_missing
from ipclick.cluster.tokens import describe as describe_tokens
from ipclick.compression import CompressionPolicy
from ipclick.dto.models import METHOD_MAP, IPClickAdapter
from ipclick.dto.proto import task_pb2, task_pb2_grpc
from ipclick.exceptions import ConfigError, TransportError
from ipclick.rpc import open_channel_for
from ipclick.services.detached import DetachedContext
from ipclick.services.task_service import FORWARD_HEADER, TaskService, is_forwarded
from ipclick.tls import TLSSettings
from ipclick.utils.config_util import Settings, section
from ipclick.utils.log_util import log
from ipclick.utils.url_util import validate_url


FORWARD_TIMEOUT_MARGIN = 15.0

DEFAULT_FORWARD_TIMEOUT = 120.0

_BROWSER_COLD_START = 60.0
_BROWSER_OVERHEAD = 15.0

_BROWSER_ADAPTER_NAMES = frozenset({"browser", "playwright", "patchright", "camoufox", "DrissionPage"})

# 只有不会改变目标资源的读取方法才能在“下游可能已经执行、只是响应丢失”时
# 自动换节点。PUT/DELETE 虽在 HTTP 规范中具有幂等语义，但实际下载目标未必严格
# 实现该保证，因此这里采用保守白名单，避免重复提交产生业务副作用。
_FAILOVER_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@final
@dataclass
class _ChannelEntry:
    """一个下游 channel 及其在途使用者计数。

    热更新（改节点地址、删节点）会关闭 channel，而在途的转发请求可能正拿着它——
    在已关闭的 channel 上构造 stub 会让进程直接段错误，在它上面发起调用则抛
    ValueError（**不是** grpc.RpcError，所以两处 except grpc.RpcError 都接不住，
    异常会穿透 servicer）。所以 close 必须推迟到最后一个使用者放手之后。
    """

    channel: grpc.Channel
    users: int = 0
    retired: bool = False


_NODE_FAULT_CODES = frozenset(
    {
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.DEADLINE_EXCEEDED,
        grpc.StatusCode.RESOURCE_EXHAUSTED,
        grpc.StatusCode.INTERNAL,
        grpc.StatusCode.UNKNOWN,
    }
)


class ForwardingTaskService(TaskService):
    """在本地执行与远端节点之间进行负载均衡的任务服务。"""

    def __init__(
        self,
        config: Settings,
        cluster: ClusterConfig | None = None,
        *,
        tls: TLSSettings | None = None,
        pool: NodePool | None = None,
        server_host: str = "",
        server_port: int = 0,
    ):
        super().__init__(config)
        self.cluster: ClusterConfig = cluster or ClusterConfig.from_config(section(config, "CLUSTER"))
        self._tls: TLSSettings = tls or TLSSettings()
        self._secret: str = cluster_secret(section(config, "CLUSTER"))

        self.self_id: str = resolve_self_id(self.cluster, server_host, server_port)
        if self.self_id:
            self.node_id: str = self.self_id

        self._pool: NodePool = pool or NodePool(self.cluster, tls=self._tls)
        self._channels: dict[str, _ChannelEntry] = {}
        self._channels_lock: threading.Lock = threading.Lock()
        self._compression: CompressionPolicy = CompressionPolicy(section(config, "CLIENT"))
        self._forwarded_count: int = 0
        self._local_count: int = 0
        self._server_host: str = server_host
        self._server_port: int = server_port
        self._reload_lock: threading.Lock = threading.Lock()

        explicit = sum(1 for n in self.cluster.nodes if n.token)
        warn_if_missing(self.self_id, len(self.cluster.nodes), self._secret, explicit)
        log.info(
            f"服务端转发已启用：{len(self.cluster.nodes)} 个节点，策略 {self._pool.balancer.name}，"
            f"本节点 {self.self_id or '未在节点列表中（只转发不执行）'}"
        )
        self_node = self.cluster.node_by_id(self.self_id)
        log.info(f"集群内部鉴权：{describe_tokens(self.self_id, self_node.token if self_node else '', self._secret)}")

    @override
    def Send(self, request: task_pb2.ReqTask, context: ServicerContext) -> task_pb2.TaskResp:
        """派发普通请求，节点级故障耗尽后由入口节点兜底执行。"""
        if not self.cluster.forwarding_enabled or is_forwarded(context):
            self._local_count += 1
            return super().Send(request, context)

        # 入口节点的 SSRF 准入必须先过一遍。本地执行时它在 prepare() 里，但被转发的
        # 请求走不到那里——于是"入口开了 block_private_networks、工作节点用默认配置"
        # 这个再常见不过的组合下，整套策略对所有被转发的请求完全不生效。
        # 下游节点自己也会校验，但入口这一层是运维实际配置、也实际以为在起作用的那一层。
        rejected = self._reject_if_url_not_allowed(request, context)
        if rejected is not None:
            return rejected

        tried: set[str] = set()
        attempts = self.cluster.max_failover + 1
        last_error: str = ""

        for attempt in range(attempts):
            try:
                state = self._pool.acquire(exclude=tried)
            except TransportError as e:
                last_error = str(e)
                break

            tried.add(state.node.id)
            if state.node.id == self.self_id:
                self._local_count += 1
                state.record_request(success=True)
                return super().Send(request, context)

            try:
                response = self._forward(state, request)
            except ValueError as e:
                # channel 在停机或热更新的边缘被关掉时，grpc 抛的是 ValueError
                # （"Cannot invoke RPC on closed channel!"）而不是 RpcError，上面那个
                # except 接不住，异常会穿透 servicer：不记链路、不换节点、不本地兜底，
                # 调用方只看到一个 UNKNOWN。引用计数已经让这条路极难走到，这里是兜底。
                last_error = f"到节点 {state.node.id} 的连接已关闭：{e}"
                state.record_request(success=False)
                if not is_failover_safe(request):
                    log.warning(f"非幂等请求 {request.uuid} 遇到连接关闭，不重投：{last_error}")
                    return self._build_error_response(request, last_error)
                log.warning(f"转发到 {state.node.id} 时连接已关闭（第 {attempt + 1}/{attempts} 次），换一台重试")
                continue
            except grpc.RpcError as e:
                last_error = rpc_detail(e)
                state.record_request(success=False)
                if is_node_fault(e):
                    if should_mark_unhealthy(e):
                        state.mark_unhealthy(last_error)
                    if not is_failover_safe(request):
                        log.warning(
                            f"节点 {state.node.id} 处理非幂等请求 {request.uuid} 时结果未知，"
                            f"为避免重复执行，不再换节点或本地兜底：{last_error}"
                        )
                        propagate_rpc_error(context, e)
                        return self._build_error_response(request, last_error)
                    log.warning(f"转发到 {state.node.id} 失败（第 {attempt + 1}/{attempts} 次）：{last_error}")
                    continue
                log.warning(f"节点 {state.node.id} 拒绝了请求 {request.uuid}：{last_error}")
                propagate_rpc_error(context, e)
                return self._build_error_response(request, last_error)

            state.record_request(success=True)
            self._forwarded_count += 1
            return response

        if self.cluster.node_by_id(self.self_id) is None:
            message = f"请求在 {len(tried)} 个节点上转发均失败，且入口节点未加入执行池：{last_error}"
            log.warning(f"请求 {request.uuid} {message}")
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            context.set_details(message)
            return self._build_error_response(request, message)

        log.warning(f"请求 {request.uuid} 在 {len(tried)} 个节点上转发均失败，改由本节点执行：{last_error}")
        self._local_count += 1
        return super().Send(request, context)

    @override
    def SendStream(self, request: task_pb2.ReqTask, context: ServicerContext) -> Iterator[task_pb2.TaskRespChunk]:
        """流式请求在入口本地执行，避免跨节点二次缓冲和续传语义变化。"""
        yield from super().SendStream(request, context)

    def send_to_node(self, request: task_pb2.ReqTask, node_id: str) -> task_pb2.TaskResp:
        """绕过负载均衡，将管理端请求定向发送到指定节点。"""
        node = self.cluster.node_by_id(node_id)
        if node is None:
            raise TransportError(f"节点 {node_id!r} 不在集群节点列表里，已有：{[n.id for n in self.cluster.nodes]}")

        if node_id == self.self_id:
            self._local_count += 1
            return super().Send(request, DetachedContext().as_servicer_context())

        state = self._pool.state_for(node_id) or NodeState(node)
        try:
            response = self._forward(state, request)
        except grpc.RpcError:
            state.record_request(success=False)
            raise
        state.record_request(success=True)
        self._forwarded_count += 1
        return response

    def reload_cluster(self, config: Settings) -> tuple[bool, str]:
        """热更新静态节点、派发策略和下游连接。"""
        cluster_section = section(config, "CLUSTER")
        with self._reload_lock:
            try:
                updated = ClusterConfig.from_config(cluster_section)
            except ConfigError as e:
                return False, f"新的集群配置不合法，已保持原样：{e}"

            previous = {n.id: n for n in self.cluster.nodes}
            try:
                added, removed = self._pool.replace(updated)
            except ConfigError as e:
                return False, f"新的集群配置不合法，已保持原样：{e}"

            # NodePool 已成功替换后再提交服务状态，避免热更新失败留下半套配置。
            self.config: Settings = config
            self.components.config = config
            self.cluster = updated
            self._secret = cluster_secret(cluster_section)

            stale = set(removed) | {
                node_id
                for node_id, old in previous.items()
                if (new := updated.node_by_id(node_id)) is not None and new.address != old.address
            }
            self._close_channels(stale)

            self.self_id = resolve_self_id(updated, self._server_host, self._server_port)
            self.node_id = self.self_id or self._recorder.node_id

        if not added and not removed and not stale:
            return True, f"集群配置已热更新（{len(updated.nodes)} 个节点，策略 {self._pool.balancer.name}）"

        parts: list[str] = []
        if added:
            parts.append(f"新增 {', '.join(added)}")
        if removed:
            parts.append(f"移除 {', '.join(removed)}")
        summary = "；".join(parts) or "地址已更新"
        log.info(f"集群配置热更新：{summary}，当前 {len(updated.nodes)} 个节点")
        return True, f"已热更新并立即生效：{summary}"

    def _reject_if_url_not_allowed(
        self, request: task_pb2.ReqTask, context: ServicerContext
    ) -> task_pb2.TaskResp | None:
        """在转发之前施加入口节点的 URL 准入；被拒时返回标准错误响应。"""
        try:
            validate_url(request.url, self.url_policy)
        except Exception as e:
            with self.track(request, context) as tr:
                tr.forwarded = True
                response = self._response_for_exception(e, request, tr, context)
                self.record_outcome(tr, response)
            return response
        return None

    def _close_channels(self, node_ids: set[str]) -> None:
        """把被删除或地址已变化节点的 channel 从缓存里摘除。

        没有在途使用者的当场关闭；还被在途请求持有的只做标记，实际 close 由最后一个
        使用者退出时执行（见 ``_leased_stub``）。
        """
        if not node_ids:
            return
        with self._channels_lock:
            for node_id in node_ids:
                entry = self._channels.pop(node_id, None)
                if entry is None:
                    continue
                if entry.users == 0:
                    entry.channel.close()
                    log.debug(f"已关闭到节点 {node_id} 的转发连接")
                else:
                    # 还有在途请求拿着它，标记待关闭，由最后一个使用者收尾。
                    entry.retired = True
                    log.debug(f"到节点 {node_id} 的转发连接有 {entry.users} 个在途请求，待其结束后关闭")

    def _forward(self, state: NodeState, request: task_pb2.ReqTask) -> task_pb2.TaskResp:
        """携带内部令牌和转发标记调用单个下游节点。"""
        node = state.node
        try:
            adapter_name = IPClickAdapter.from_pb(request.adapter).display_name
        except ValueError:
            adapter_name = "unknown"
        method = METHOD_MAP.get(request.method, "GET")

        with self._recorder.track_request(adapter_name, method, uuid=request.uuid, url=request.url) as tr:
            tr.forwarded = True
            tr.node_id = node.id

            metadata = ((FORWARD_HEADER, "1"), *build_client_metadata(token_for(node.id, node.token, self._secret)))
            with self._leased_stub(node.id, node.host, node.port) as stub:
                response = stub.Send(
                    request,
                    timeout=self._timeout_for(request),
                    metadata=metadata,
                    compression=self._compression.for_request(request),
                )

            tr.status_code = response.status_code
            tr.size = len(response.content)
            tr.error = response.error_message
            if response.HasField("trace"):
                tr.node_id = response.trace.node_id or node.id
                tr.adapter = response.trace.adapter or adapter_name
                tr.attempts = response.trace.attempts or 1
                tr.queued_ms = response.trace.queued_ms
            return response

    def _timeout_for(self, request: task_pb2.ReqTask) -> float:
        """根据下载、重试及浏览器冷启动预算推导下游 RPC 超时。"""
        if self.cluster.forward_timeout > 0:
            return self.cluster.forward_timeout

        base = (
            float(request.timeout_seconds)
            if request.HasField("timeout_seconds") and request.timeout_seconds > 0
            else self.adapter_settings.download_timeout
        )
        if base <= 0:
            return DEFAULT_FORWARD_TIMEOUT

        if self._is_browser_request(request):
            base = max(base, self._browser_budget())
        retries = request.max_retries if request.HasField("max_retries") else self.adapter_settings.max_attempts
        # 下游适配器会为每次重试重新消耗一次下载预算，RPC 外层需覆盖完整尝试链。
        return base * max(1, retries + 1) + FORWARD_TIMEOUT_MARGIN

    @staticmethod
    def _is_browser_request(request: task_pb2.ReqTask) -> bool:
        try:
            member = IPClickAdapter.from_pb(request.adapter)
        except ValueError:
            return False
        return member.display_name in _BROWSER_ADAPTER_NAMES

    def _browser_budget(self) -> float:
        s = self.browser_settings
        return s.page_load_timeout + s.script_timeout + _BROWSER_COLD_START + _BROWSER_OVERHEAD

    @contextmanager
    def _leased_stub(self, node_id: str, host: str, port: int) -> Generator[Any]:
        """租借一个下游 stub，并在使用期间阻止它的 channel 被关闭。

        stub 的构造也放在锁内：在一个已被 close() 的 channel 上构造 stub 会触发
        grpc 的 registered-call 查找，实测直接 SIGSEGV——那是信号杀进程，Python
        层面接不住，所以只能靠不让它发生。
        """
        with self._channels_lock:
            entry = self._channels.get(node_id)
            if entry is None:
                target_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
                target = f"{target_host}:{port}"
                entry = _ChannelEntry(open_channel_for(target, self._tls))
                self._channels[node_id] = entry
                log.debug(f"已建立到节点 {node_id} ({target}) 的转发连接")
            entry.users += 1
            stub = task_pb2_grpc.TaskServiceStub(entry.channel)
        try:
            yield stub
        finally:
            with self._channels_lock:
                entry.users -= 1
                if entry.retired and entry.users == 0:
                    entry.channel.close()
                    log.debug(f"到节点 {node_id} 的转发连接已在最后一个在途请求结束后关闭")

    @property
    @override
    def forward_enabled(self) -> bool:
        """返回当前配置是否实际启用服务端转发。"""
        return self.cluster.forwarding_enabled

    def snapshot(self) -> dict[str, Any]:
        """在节点池快照上附加转发服务计数和身份信息。"""
        data = self._pool.snapshot()
        data.update(
            {
                "forward": self.cluster.forwarding_enabled,
                "self_id": self.self_id,
                "self_in_pool": bool(self.cluster.node_by_id(self.self_id)),
                "forwarded_requests": self._forwarded_count,
                "local_requests": self._local_count,
                "internal_auth": bool(self._secret) or any(n.token for n in self.cluster.nodes),
            }
        )
        return data

    def set_drained(self, node_id: str, drained: bool) -> bool:
        """由管理端手动摘除或恢复节点。"""
        return self._pool.drain(node_id) if drained else self._pool.undrain(node_id)

    @override
    def cleanup(self) -> None:
        """停止探活、摘除下游 channel（在途请求结束后关闭），再释放本地适配器。"""
        self._pool.stop()
        self._close_channels(set(self._channels))
        super().cleanup()

    @override
    async def acleanup(self) -> None:
        """异步服务停机时同步释放集群资源并异步关闭适配器。

        channel 同样是先摘除、由最后一个在途请求收尾关闭，不会在调用中途 close 掉。
        """
        self._pool.stop()
        self._close_channels(set(self._channels))
        await super().acleanup()


def resolve_self_id(cluster: ClusterConfig, server_host: str = "", server_port: int = 0) -> str:
    """优先使用显式 ID，否则按本机地址和监听端口识别当前节点。"""
    if cluster.self_id:
        if cluster.nodes and cluster.node_by_id(cluster.self_id) is None:
            log.warning(
                f"[CLUSTER].self_id = {cluster.self_id!r} 不在 nodes 列表里，本节点不会参与轮询。"
                f"已有 id：{[n.id for n in cluster.nodes]}"
            )
        return cluster.self_id

    if not cluster.nodes or not server_port:
        return ""

    local = _local_addresses()
    matches = [n.id for n in cluster.nodes if n.port == server_port and n.host.lower() in local]
    if len(matches) == 1:
        log.info(f"自动识别本节点为 {matches[0]}（按监听地址 {server_host}:{server_port} 匹配）")
        return matches[0]
    if len(matches) > 1:
        log.warning(f"有多个节点匹配本机监听地址（{matches}），请显式设置 [CLUSTER].self_id")
        return ""
    if cluster.forwarding_enabled:
        log.warning(
            f"未能在 nodes 里识别出本节点（监听 {server_host}:{server_port}）。"
            f"本节点将只转发、不执行任务；如需参与轮询请设置 [CLUSTER].self_id"
        )
    else:
        log.debug(f"未能在 nodes 里识别出本节点（监听 {server_host}:{server_port}），但转发未开启，无影响")
    return ""


def _local_addresses() -> set[str]:
    import socket

    addresses: set[str] = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "[::]", ""}
    try:
        hostname = socket.gethostname()
        addresses.add(hostname.lower())
        _, _, ips = socket.gethostbyname_ex(hostname)
        addresses.update(ip.lower() for ip in ips)
    except OSError as e:
        log.debug(f"取本机地址失败，自动识别节点身份可能不准：{e}")
    return addresses


def rpc_detail(error: grpc.RpcError) -> str:
    """提取稳定的 gRPC 状态名称与详情文本。"""
    code = getattr(error, "code", lambda: None)()
    details = getattr(error, "details", lambda: "")() or ""
    name = getattr(code, "name", str(code))
    return f"{name}: {details}" if details else str(name)


def is_node_fault(error: grpc.RpcError) -> bool:
    """判断错误是否表示可通过切换节点恢复的故障。"""
    code = getattr(error, "code", lambda: None)()
    return code in _NODE_FAULT_CODES


def is_failover_safe(request: task_pb2.ReqTask) -> bool:
    """仅允许无副作用的读取方法在结果未知时自动重投。"""
    return METHOD_MAP.get(request.method, "") in _FAILOVER_SAFE_METHODS


def should_mark_unhealthy(error: grpc.RpcError) -> bool:
    """判断错误是否足以立即摘除节点。"""
    code = getattr(error, "code", lambda: None)()
    return code is grpc.StatusCode.UNAVAILABLE


def propagate_rpc_error(context: ServicerContext, error: grpc.RpcError) -> None:
    """把下游非节点故障的状态和详情复制到入口调用。"""
    code = getattr(error, "code", lambda: None)()
    if code is not None:
        context.set_code(code)
    context.set_details(rpc_detail(error))


__all__ = ["ForwardingTaskService", "resolve_self_id"]
