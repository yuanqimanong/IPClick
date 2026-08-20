"""集群节点配置、健康状态和运行计数模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import threading
import time
from typing import Any

from ipclick.exceptions import ConfigError
from ipclick.utils.coerce import as_float, as_int, as_positive_float, as_text, as_text_tuple


class NodeStatus(StrEnum):
    """节点最近一次稳定判定的健康状态。"""

    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"


@dataclass(frozen=True)
class Node:
    """一个可被派发请求的 gRPC 节点。"""

    id: str
    host: str
    port: int
    weight: int = 100
    region: str = ""
    zone: str = ""
    tags: tuple[str, ...] = ()
    token: str = ""

    @property
    def address(self) -> str:
        """返回包含端口且兼容 IPv6 的 gRPC 地址。"""
        # gRPC target 中的 IPv6 字面量必须使用方括号，避免最后一段被误认作端口。
        host = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        return f"{host}:{self.port}"

    @classmethod
    def from_config(cls, entry: dict[str, Any], index: int = 0) -> Node:
        """从 ``host:port`` 配置项构造节点，并校验地址和端口。"""
        address = str(entry.get("address", "")).strip()
        if not address:
            raise ConfigError(f"集群节点 #{index} 缺少 address")

        host, sep, port_text = address.rpartition(":")
        if not sep or not host:
            raise ConfigError(f"集群节点 {address!r} 格式应为 host:port")
        try:
            port = int(port_text)
        except ValueError as e:
            raise ConfigError(f"集群节点 {address!r} 的端口不是数字") from e
        if not 1 <= port <= 65535:
            raise ConfigError(f"集群节点 {address!r} 的端口必须在 1..65535 范围内")

        return cls(
            id=as_text(entry.get("id"), address),
            host=host.strip("[]"),
            port=port,
            weight=as_int(entry.get("weight"), 100, minimum=1),
            region=as_text(entry.get("region")),
            zone=as_text(entry.get("zone")),
            tags=as_text_tuple(entry.get("tags")),
            token=as_text(entry.get("token")),
        )


class NodeState:
    """线程安全地维护节点健康判定和请求计数。"""

    def __init__(self, node: Node):
        self.node: Node = node
        self._lock: threading.Lock = threading.Lock()

        self._status: NodeStatus = NodeStatus.UNKNOWN
        self._consecutive_failures: int = 0
        self._consecutive_successes: int = 0
        self._last_checked: float = 0.0
        self._last_error: str = ""
        self._total_requests: int = 0
        self._total_failures: int = 0

    @property
    def status(self) -> NodeStatus:
        """返回最近一次稳定健康判定。"""
        with self._lock:
            return self._status

    @property
    def is_available(self) -> bool:
        """返回节点是否尚未被判定为不健康。"""
        with self._lock:
            return self._status is not NodeStatus.UNHEALTHY

    def snapshot(self) -> dict[str, Any]:
        """返回适合状态页和诊断接口序列化的状态快照。"""
        with self._lock:
            return {
                "id": self.node.id,
                "address": self.node.address,
                "weight": self.node.weight,
                "region": self.node.region,
                "zone": self.node.zone,
                "tags": list(self.node.tags),
                "status": self._status.value,
                "consecutive_failures": self._consecutive_failures,
                "last_checked": self._last_checked,
                "last_checked_ago": (time.time() - self._last_checked) if self._last_checked else None,
                "last_error": self._last_error,
                "total_requests": self._total_requests,
                "total_failures": self._total_failures,
            }

    def record_probe(self, healthy: bool, detail: str, *, failure_threshold: int, recovery_threshold: int) -> bool:
        """记录探活结果；仅达到迟滞阈值时切换状态并返回 ``True``。"""
        with self._lock:
            previous = self._status
            self._last_checked = time.time()

            if healthy:
                self._consecutive_successes += 1
                self._consecutive_failures = 0
                self._last_error = ""
                if self._status is not NodeStatus.HEALTHY and self._consecutive_successes >= recovery_threshold:
                    self._status = NodeStatus.HEALTHY
            else:
                self._consecutive_failures += 1
                self._consecutive_successes = 0
                self._last_error = detail
                if self._status is not NodeStatus.UNHEALTHY and self._consecutive_failures >= failure_threshold:
                    self._status = NodeStatus.UNHEALTHY

            return self._status is not previous

    def record_request(self, success: bool) -> None:
        """累计一次请求及其失败计数。"""
        with self._lock:
            self._total_requests += 1
            if not success:
                self._total_failures += 1

    def mark_unhealthy(self, reason: str) -> None:
        """立即将节点标记为不健康并记录原因。"""
        with self._lock:
            self._status = NodeStatus.UNHEALTHY
            self._last_error = reason
            self._consecutive_successes = 0

    def update_node(self, node: Node) -> None:
        """原子替换节点的地址、权重和凭据配置。"""
        with self._lock:
            self.node = node


@dataclass
class ClusterConfig:
    """集群发现、负载均衡、探活及服务端转发配置。"""

    nodes: tuple[Node, ...] = ()
    strategy: str = "round_robin"
    failure_threshold: int = 2
    recovery_threshold: int = 2
    probe_interval: float = 10.0
    probe_timeout: float = 3.0
    max_failover: int = 2
    forward: str = "off"
    self_id: str = ""
    forward_timeout: float = 0.0
    secret: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def forwarding_enabled(self) -> bool:
        """返回服务端转发开关与静态节点是否同时就绪。"""
        return self.forward == "on" and bool(self.nodes)

    def node_by_id(self, node_id: str) -> Node | None:
        """按节点 ID 查找静态配置节点。"""
        return next((n for n in self.nodes if n.id == node_id), None)

    @property
    def enabled(self) -> bool:
        """返回是否配置了至少一个静态节点。"""
        return bool(self.nodes)

    @classmethod
    def from_config(cls, cluster_config: dict[str, Any] | None) -> ClusterConfig:
        """解析集群配置，并拒绝会导致路由歧义的重复节点 ID。"""
        config = dict(cluster_config or {})

        raw_nodes = config.get("nodes") or []
        nodes: list[Node] = []
        if isinstance(raw_nodes, (list, tuple)):
            for index, entry in enumerate(raw_nodes):
                if isinstance(entry, dict):
                    nodes.append(Node.from_config(dict(entry), index))

        ids = [node.id for node in nodes]
        duplicates = sorted({node_id for node_id in ids if ids.count(node_id) > 1})
        if duplicates:
            raise ConfigError(f"集群节点 id 不可重复：{', '.join(duplicates)}")

        defaults = cls()

        forward = as_text(config.get("forward"), defaults.forward).lower()
        if forward in ("true", "yes", "1"):
            forward = "on"
        elif forward in ("false", "no", "0"):
            forward = "off"
        if forward not in FORWARD_MODES:
            raise ConfigError(f"未知的 [CLUSTER].forward {forward!r}，可选：{'、'.join(sorted(FORWARD_MODES))}")

        return cls(
            nodes=tuple(nodes),
            strategy=as_text(config.get("load_balancer"), defaults.strategy).lower(),
            failure_threshold=as_int(config.get("failure_threshold"), defaults.failure_threshold, minimum=1),
            recovery_threshold=as_int(config.get("recovery_threshold"), defaults.recovery_threshold, minimum=1),
            probe_interval=as_positive_float(config.get("probe_interval"), defaults.probe_interval),
            probe_timeout=as_positive_float(config.get("probe_timeout"), defaults.probe_timeout),
            max_failover=as_int(config.get("max_failover"), defaults.max_failover, minimum=0),
            forward=forward,
            self_id=as_text(config.get("self_id")),
            forward_timeout=as_float(config.get("forward_timeout"), defaults.forward_timeout, minimum=0.0),
            secret=as_text(config.get("secret")),
            extra={k: v for k, v in config.items() if k not in _KNOWN_KEYS},
        )


FORWARD_MODES = frozenset({"off", "on"})


_KNOWN_KEYS = frozenset(
    {
        "nodes",
        "load_balancer",
        "failure_threshold",
        "recovery_threshold",
        "probe_interval",
        "probe_timeout",
        "max_failover",
        "forward",
        "self_id",
        "forward_timeout",
        "secret",
    }
)


__all__ = ["FORWARD_MODES", "ClusterConfig", "Node", "NodeState", "NodeStatus"]
