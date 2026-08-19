from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import threading
import time
from typing import Any

from ipclick.exceptions import ConfigError


class NodeStatus(StrEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"


@dataclass(frozen=True)
class Node:
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
        return f"{self.host}:{self.port}"

    @classmethod
    def from_config(cls, entry: dict[str, Any], index: int = 0) -> Node:
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

        weight = entry.get("weight", 100)
        try:
            weight = max(1, int(weight))
        except (TypeError, ValueError):
            weight = 100

        raw_tags = entry.get("tags") or ()
        tags = tuple(str(t) for t in raw_tags) if isinstance(raw_tags, (list, tuple, set)) else ()

        return cls(
            id=str(entry.get("id") or address),
            host=host.strip("[]"),
            port=port,
            weight=weight,
            region=str(entry.get("region", "")),
            zone=str(entry.get("zone", "")),
            tags=tags,
            token=str(entry.get("token", "") or ""),
        )


class NodeState:
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
        with self._lock:
            return self._status

    @property
    def is_available(self) -> bool:
        with self._lock:
            return self._status is not NodeStatus.UNHEALTHY

    def snapshot(self) -> dict[str, Any]:
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
        with self._lock:
            self._total_requests += 1
            if not success:
                self._total_failures += 1

    def mark_unhealthy(self, reason: str) -> None:
        with self._lock:
            self._status = NodeStatus.UNHEALTHY
            self._last_error = reason
            self._consecutive_successes = 0

    def update_node(self, node: Node) -> None:
        with self._lock:
            self.node = node


@dataclass
class ClusterConfig:
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
        return self.forward == "on" and bool(self.nodes)

    def node_by_id(self, node_id: str) -> Node | None:
        return next((n for n in self.nodes if n.id == node_id), None)

    @property
    def enabled(self) -> bool:
        return bool(self.nodes)

    @classmethod
    def from_config(cls, cluster_config: dict[str, Any] | None) -> ClusterConfig:
        config = dict(cluster_config or {})

        raw_nodes = config.get("nodes") or []
        nodes: list[Node] = []
        if isinstance(raw_nodes, (list, tuple)):
            for index, entry in enumerate(raw_nodes):
                if isinstance(entry, dict):
                    nodes.append(Node.from_config(dict(entry), index))

        defaults = cls()

        def _num(key: str, fallback: float) -> float:
            try:
                value = float(config.get(key, fallback))
            except (TypeError, ValueError):
                return fallback
            return value if value > 0 else fallback

        def _count(key: str, fallback: int) -> int:
            try:
                value = int(config.get(key, fallback))
            except (TypeError, ValueError):
                return fallback
            return value if value >= 1 else fallback

        forward = str(config.get("forward", defaults.forward) or defaults.forward).strip().lower()
        if forward in ("true", "yes", "1"):
            forward = "on"
        elif forward in ("false", "no", "0"):
            forward = "off"
        if forward not in FORWARD_MODES:
            raise ConfigError(f"未知的 [CLUSTER].forward {forward!r}，可选：{'、'.join(sorted(FORWARD_MODES))}")

        return cls(
            nodes=tuple(nodes),
            strategy=str(config.get("load_balancer", defaults.strategy) or defaults.strategy).lower(),
            failure_threshold=_count("failure_threshold", defaults.failure_threshold),
            recovery_threshold=_count("recovery_threshold", defaults.recovery_threshold),
            probe_interval=_num("probe_interval", defaults.probe_interval),
            probe_timeout=_num("probe_timeout", defaults.probe_timeout),
            max_failover=_count("max_failover", defaults.max_failover),
            forward=forward,
            self_id=str(config.get("self_id", "") or ""),
            forward_timeout=max(0.0, _as_float(config.get("forward_timeout"), defaults.forward_timeout)),
            secret=str(config.get("secret", "") or ""),
            extra={k: v for k, v in config.items() if k not in _KNOWN_KEYS},
        )


def _as_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


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
