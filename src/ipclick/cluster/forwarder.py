from __future__ import annotations

from collections.abc import Iterator
import threading
from typing import Any

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


FORWARD_TIMEOUT_MARGIN = 15.0

DEFAULT_FORWARD_TIMEOUT = 120.0

_BROWSER_COLD_START = 60.0
_BROWSER_OVERHEAD = 15.0

_BROWSER_ADAPTER_NAMES = frozenset({"browser", "playwright", "patchright", "camoufox", "DrissionPage"})

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
        self._channels: dict[str, grpc.Channel] = {}
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
        if is_forwarded(context):
            self._local_count += 1
            return super().Send(request, context)

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
            except grpc.RpcError as e:
                last_error = _rpc_detail(e)
                state.record_request(success=False)
                if _is_node_fault(e):
                    if _should_mark_unhealthy(e):
                        state.mark_unhealthy(last_error)
                    log.warning(f"转发到 {state.node.id} 失败（第 {attempt + 1}/{attempts} 次）：{last_error}")
                    continue
                log.warning(f"节点 {state.node.id} 拒绝了请求 {request.uuid}：{last_error}")
                _propagate(context, e)
                return self._build_error_response(request, last_error)

            state.record_request(success=True)
            self._forwarded_count += 1
            return response

        log.warning(f"请求 {request.uuid} 在 {len(tried)} 个节点上转发均失败，改由本节点执行：{last_error}")
        self._local_count += 1
        return super().Send(request, context)

    @override
    def SendStream(self, request: task_pb2.ReqTask, context: ServicerContext) -> Iterator[task_pb2.TaskRespChunk]:
        yield from super().SendStream(request, context)

    def send_to_node(self, request: task_pb2.ReqTask, node_id: str) -> task_pb2.TaskResp:
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
        cluster_section = section(config, "CLUSTER")
        with self._reload_lock:
            try:
                updated = ClusterConfig.from_config(cluster_section)
            except ConfigError as e:
                return False, f"新的集群配置不合法，已保持原样：{e}"

            previous = {n.id: n for n in self.cluster.nodes}
            self.config: Settings = config
            self.cluster = updated
            self._secret = cluster_secret(cluster_section)

            added, removed = self._pool.replace(updated)

            stale = set(removed) | {
                node_id
                for node_id, old in previous.items()
                if (new := updated.node_by_id(node_id)) is not None and new.address != old.address
            }
            self._close_channels(stale)

            self.self_id = resolve_self_id(updated, self._server_host, self._server_port)
            if self.self_id:
                self.node_id = self.self_id

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

    def _close_channels(self, node_ids: set[str]) -> None:
        if not node_ids:
            return
        with self._channels_lock:
            for node_id in node_ids:
                channel = self._channels.pop(node_id, None)
                if channel is not None:
                    channel.close()
                    log.debug(f"已关闭到节点 {node_id} 的转发连接")

    def _forward(self, state: NodeState, request: task_pb2.ReqTask) -> task_pb2.TaskResp:
        node = state.node
        try:
            adapter_name = IPClickAdapter.from_pb(request.adapter).display_name
        except ValueError:
            adapter_name = "unknown"
        method = METHOD_MAP.get(request.method, "GET")

        with self._recorder.track_request(adapter_name, method, uuid=request.uuid, url=request.url) as tr:
            tr.forwarded = True
            tr.node_id = node.id

            stub = task_pb2_grpc.TaskServiceStub(self._channel_for(node.id, node.host, node.port))
            metadata = ((FORWARD_HEADER, "1"), *build_client_metadata(token_for(node.id, node.token, self._secret)))
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

    def _channel_for(self, node_id: str, host: str, port: int) -> grpc.Channel:
        channel = self._channels.get(node_id)
        if channel is not None:
            return channel
        with self._channels_lock:
            if node_id not in self._channels:
                target = f"{host}:{port}"
                self._channels[node_id] = open_channel_for(target, self._tls)
                log.debug(f"已建立到节点 {node_id} ({target}) 的转发连接")
            return self._channels[node_id]

    @property
    @override
    def forward_enabled(self) -> bool:
        return True

    def snapshot(self) -> dict[str, Any]:
        data = self._pool.snapshot()
        data.update(
            {
                "forward": True,
                "self_id": self.self_id,
                "self_in_pool": bool(self.cluster.node_by_id(self.self_id)),
                "forwarded_requests": self._forwarded_count,
                "local_requests": self._local_count,
                "internal_auth": bool(self._secret) or any(n.token for n in self.cluster.nodes),
            }
        )
        return data

    @override
    def cleanup(self) -> None:
        self._pool.stop()
        with self._channels_lock:
            for channel in self._channels.values():
                channel.close()
            self._channels.clear()
        super().cleanup()


def resolve_self_id(cluster: ClusterConfig, server_host: str = "", server_port: int = 0) -> str:
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


def _rpc_detail(error: grpc.RpcError) -> str:
    code = getattr(error, "code", lambda: None)()
    details = getattr(error, "details", lambda: "")() or ""
    name = getattr(code, "name", str(code))
    return f"{name}: {details}" if details else str(name)


def _is_node_fault(error: grpc.RpcError) -> bool:
    code = getattr(error, "code", lambda: None)()
    return code in _NODE_FAULT_CODES


def _should_mark_unhealthy(error: grpc.RpcError) -> bool:
    code = getattr(error, "code", lambda: None)()
    return code is grpc.StatusCode.UNAVAILABLE


def _propagate(context: ServicerContext, error: grpc.RpcError) -> None:
    code = getattr(error, "code", lambda: None)()
    if code is not None:
        context.set_code(code)
    context.set_details(_rpc_detail(error))


__all__ = ["ForwardingTaskService", "resolve_self_id"]
