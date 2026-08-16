"""集群节点模型与状态。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import threading
import time
from typing import Any

from ipclick.exceptions import ConfigError


class NodeStatus(StrEnum):
    """节点状态。

    ``UNKNOWN`` 是刚启动、还没探过活的状态。它被当作**可用**处理——
    否则冷启动的一瞬间所有节点都不可用，第一批请求会全部失败。
    """

    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"


@dataclass(frozen=True)
class Node:
    """一个 IPClick 服务端节点。"""

    id: str
    host: str
    port: int
    weight: int = 100
    region: str = ""
    zone: str = ""
    tags: tuple[str, ...] = ()
    #: 调用该节点时使用的令牌。留空则由 [CLUSTER].secret 派生
    #: （见 :mod:`ipclick.cluster.tokens`），这样加节点不需要发放新凭据。
    token: str = ""

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    @classmethod
    def from_config(cls, entry: dict[str, Any], index: int = 0) -> Node:
        """从 ``[CLUSTER].nodes`` 的一项构造。

        Raises:
            ConfigError: address 缺失或格式不合法。
        """
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
            host=host.strip("[]"),  # 兼容 IPv6 字面量写法 [::1]:9527
            port=port,
            weight=weight,
            region=str(entry.get("region", "")),
            zone=str(entry.get("zone", "")),
            tags=tags,
            token=str(entry.get("token", "") or ""),
        )


class NodeState:
    """单个节点的运行时状态。

    与 :class:`Node` 分开：Node 是配置（不可变），这里是随时间变化的部分。
    所有字段都在锁内读写——健康探测在后台线程，选节点在请求线程。
    """

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

    # ---------------------------------------------------------------- #
    # 读
    # ---------------------------------------------------------------- #

    @property
    def status(self) -> NodeStatus:
        with self._lock:
            return self._status

    @property
    def is_available(self) -> bool:
        """能否派发请求。UNKNOWN 视为可用，理由见 :class:`NodeStatus`。"""
        with self._lock:
            return self._status is not NodeStatus.UNHEALTHY

    def snapshot(self) -> dict[str, Any]:
        """给状态页 / 日志用的只读快照。"""
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

    # ---------------------------------------------------------------- #
    # 写
    # ---------------------------------------------------------------- #

    def record_probe(self, healthy: bool, detail: str, *, failure_threshold: int, recovery_threshold: int) -> bool:
        """记录一次健康探测结果。

        用连续次数阈值而不是单次结果来切换状态：一次探测抖动就把节点摘掉、
        下一次又加回来，会让流量在节点间反复横跳（flapping）。

        Returns:
            状态是否发生了变化。
        """
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
        """记录一次真实请求的结果（用于统计与快速失败判断）。"""
        with self._lock:
            self._total_requests += 1
            if not success:
                self._total_failures += 1

    def mark_unhealthy(self, reason: str) -> None:
        """请求侧发现节点不可用时立刻摘除，不等下一轮探测。"""
        with self._lock:
            self._status = NodeStatus.UNHEALTHY
            self._last_error = reason
            self._consecutive_successes = 0

    def update_node(self, node: Node) -> None:
        """换掉配置部分（地址 / 权重变了），**保留**运行时状态。

        热更新节点列表时用。重建一个 NodeState 会把健康计数清零，那样
        "连续 N 次才切状态"的判定永远达不到，熔断与恢复双双失效。
        """
        with self._lock:
            self.node = node


@dataclass
class ClusterConfig:
    """``[CLUSTER]`` 配置。"""

    nodes: tuple[Node, ...] = ()
    strategy: str = "round_robin"
    #: 连续探测失败多少次才摘除节点
    failure_threshold: int = 2
    #: 连续探测成功多少次才把节点加回来
    recovery_threshold: int = 2
    #: 探活间隔（秒）
    probe_interval: float = 10.0
    #: 单次探活超时（秒）
    probe_timeout: float = 3.0
    #: 一次请求最多换几个节点重试
    max_failover: int = 2
    #: 服务端转发开关。见 :data:`FORWARD_MODES`。
    forward: str = "off"
    #: 本节点在 nodes 里的 id。留空则按 [SERVER] 的监听地址自动识别。
    self_id: str = ""
    #: 转发一次请求的超时（秒）。0 = 按任务自身超时 + 余量自动推算。
    forward_timeout: float = 0.0
    #: 集群共享密钥（机密，正规位置是 .env 的 IPCLICK_CLUSTER_SECRET）
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
        """从配置文件的 ``[CLUSTER]`` 节构造。节点列表为空即视为未启用集群。"""
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
            # 这个可以是 0（表示自动推算），所以不能用 _num（它把 0 当非法）
            forward_timeout=max(0.0, _as_float(config.get("forward_timeout"), defaults.forward_timeout)),
            secret=str(config.get("secret", "") or ""),
            extra={k: v for k, v in config.items() if k not in _KNOWN_KEYS},
        )


def _as_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


#: ``[CLUSTER].forward`` 的取值。
#:
#: * ``off``（默认）——服务端不转发。任务由谁收到就由谁执行；分发交给客户端侧的
#:   :class:`~ipclick.cluster.client.ClusterDownloader`。
#: * ``on`` ——服务端转发。入口节点按策略挑一个节点，挑到自己就本地执行、挑到
#:   别人就把 ReqTask 原样转过去。调用方只需要知道一个地址。
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
