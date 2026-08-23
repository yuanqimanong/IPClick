"""节点池、后台探活和故障节点摘除。"""

from __future__ import annotations

from collections.abc import Callable
import threading
import time
from typing import Any

from ipclick.cluster.balancer import LoadBalancer, create_balancer
from ipclick.cluster.discovery import Discovery, DiscoveryConfig, StaticDiscovery
from ipclick.cluster.node import ClusterConfig, NodeState, NodeStatus
from ipclick.exceptions import ConfigError, TransportError
from ipclick.health import check_health
from ipclick.tls import TLSSettings
from ipclick.utils.log_util import log


class NodePool:
    """协调节点发现、健康状态与负载均衡选择。"""

    def __init__(
        self,
        config: ClusterConfig,
        *,
        start_probing: bool = True,
        tls: TLSSettings | None = None,
        discovery: Discovery | None = None,
        discovery_config: DiscoveryConfig | None = None,
    ):
        self.config: ClusterConfig = config
        self.balancer: LoadBalancer = create_balancer(config.strategy)
        self._lock: threading.Lock = threading.Lock()

        self._discovery: Discovery = discovery or StaticDiscovery(config.nodes)
        self._discovery_config: DiscoveryConfig = discovery_config or DiscoveryConfig()
        self._last_refresh: float = time.monotonic()

        initial = self._discovery.resolve()
        if not initial:
            raise ConfigError("集群模式需要至少一个节点（[CLUSTER].nodes 或 [CLUSTER.discovery]）")
        self._states: list[NodeState] = [NodeState(node) for node in initial]
        self._drained: set[str] = set()

        self._tls: TLSSettings = tls or TLSSettings()

        self._stop: threading.Event = threading.Event()
        self._thread: threading.Thread | None = None
        self._health_callbacks: list[Callable[[int], None]] = []
        if start_probing:
            self.start()

    def start(self) -> None:
        """幂等启动后台探活线程。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._probe_loop, name="ipclick-health-probe", daemon=True)
        self._thread.start()
        log.debug(f"集群探活已启动，{len(self._states)} 个节点，间隔 {self.config.probe_interval}s")

    def stop(self) -> None:
        """请求探活线程停止，并等待其退出。"""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            # 当前可能正阻塞在单次探测，等待窗口至少覆盖 probe_timeout。
            timeout = max(self.config.probe_interval, self.config.probe_timeout) + 2
            thread.join(timeout=timeout)
        self._thread = None

    def _probe_loop(self) -> None:
        self.probe_once()
        self._notify_health()
        while not self._stop.wait(self.config.probe_interval):
            self.refresh_nodes()
            self.probe_once()
            self._notify_health()

    def on_health_change(self, callback: Callable[[int], None]) -> None:
        """注册健康节点数量变化后的配额回调。"""
        with self._lock:
            self._health_callbacks.append(callback)

    def _notify_health(self) -> None:
        with self._lock:
            states = list(self._states)
            callbacks = list(self._health_callbacks)
            drained = set(self._drained)
        # 计入"未被判定为不健康"的节点，而不是只算 HEALTHY。这个计数唯一的用途是限流
        # 分片（每台分 1/N 的 per_host_qps），而节点的初始状态是 UNKNOWN——只认 HEALTHY
        # 的话，从启动到第一轮探活完成的这段窗口里计数是 0，分片直接不生效，
        # **每台都按完整 QPS 跑**，N 台集群就是 N 倍的全局限额打到目标站点上。
        # 限流这件事上，"没证据说它挂了就假定它在"是更安全的方向：宁可分得偏保守。
        live = sum(1 for state in states if state.status is not NodeStatus.UNHEALTHY and state.node.id not in drained)
        for callback in callbacks:
            try:
                callback(live)
            except Exception as e:
                log.warning(f"健康状态回调失败：{e}")

    def refresh_nodes(self, *, force: bool = False) -> bool:
        """按发现刷新周期更新节点，失败或空结果时保留现有列表。"""
        interval = self._discovery_config.refresh_interval
        if not force and interval <= 0:
            return False
        now = time.monotonic()
        if not force and now - self._last_refresh < interval:
            return False
        self._last_refresh = now

        try:
            nodes = self._discovery.resolve()
        except Exception as e:
            log.warning(f"刷新集群节点失败，沿用当前列表：{e}")
            return False
        if not nodes:
            log.warning("节点发现返回空列表，沿用当前列表")
            return False

        with self._lock:
            existing = {state.node.id: state for state in self._states}
            refreshed: list[NodeState] = []
            changed = False
            for node in nodes:
                state = existing.get(node.id)
                if state is None:
                    refreshed.append(NodeState(node))
                    continue
                if state.node != node:
                    # 发现器可能保留稳定 ID、只更新地址或权重；必须同步到运行状态。
                    state.update_node(node)
                    changed = True
                refreshed.append(state)
            added = [n.id for n in nodes if n.id not in existing]
            current_ids = {n.id for n in nodes}
            removed = [i for i in existing if i not in current_ids]
            if not added and not removed and not changed:
                return False
            self._states = refreshed

        if added:
            log.info(f"集群新增节点：{', '.join(added)}")
        if removed:
            log.info(f"集群移除节点：{', '.join(removed)}")
        return True

    def replace(self, config: ClusterConfig) -> tuple[list[str], list[str]]:
        """原子替换静态节点与均衡策略，并尽量保留已有健康状态。"""
        # 先验证策略。创建失败时不能留下“新配置 + 旧均衡器”的半更新状态。
        balancer = create_balancer(config.strategy)
        with self._lock:
            self.config = config
            self.balancer = balancer

            if not isinstance(self._discovery, StaticDiscovery):
                log.debug("节点来自 DNS 发现，本次只更新策略与阈值，不改节点列表")
                return [], []

            self._discovery = StaticDiscovery(config.nodes)
            existing = {state.node.id: state for state in self._states}
            added = [n.id for n in config.nodes if n.id not in existing]
            removed = [i for i in existing if i not in {n.id for n in config.nodes}]
            refreshed: list[NodeState] = []
            for node in config.nodes:
                state = existing.get(node.id)
                if state is None:
                    refreshed.append(NodeState(node))
                    continue
                state.update_node(node)
                refreshed.append(state)
            self._states = refreshed

            # 摘除标记必须跟着节点列表一起裁剪。不裁的话：把一个已摘除的节点从配置里删掉、
            # 之后再加回来，它的 id 仍留在 _drained 里，available() 一直把它滤掉——
            # 新加的节点永远分不到流量，而快照里只显示它"已摘除"，看不出为什么。
            # DNS 发现下 id 还会不断变化，这个集合只增不减。
            self._drained &= {n.id for n in config.nodes}

        if added:
            log.info(f"集群新增节点：{', '.join(added)}")
        if removed:
            log.info(f"集群移除节点：{', '.join(removed)}")
        if added or removed:
            # 节点数变了就要立刻重算限流分片，否则配额一直停在旧的 N 上，
            # 直到下一轮探活（最长一个 probe_interval）才纠正。
            self._notify_health()
        return added, removed

    def probe_once(self) -> None:
        """同步探测当前快照中的每个节点并更新迟滞状态。"""
        with self._lock:
            states = list(self._states)
        for state in states:
            if self._stop.is_set():
                return
            healthy, detail = check_health(state.node.address, timeout=self.config.probe_timeout, tls=self._tls)
            changed = state.record_probe(
                healthy,
                detail,
                failure_threshold=self.config.failure_threshold,
                recovery_threshold=self.config.recovery_threshold,
            )
            if changed:
                level = log.info if healthy else log.warning
                level(f"节点 {state.node.id} ({state.node.address}) -> {state.status.value}: {detail}")

    def available(self) -> list[NodeState]:
        """返回未被明确标记为不健康的节点。"""
        with self._lock:
            return [s for s in self._states if s.is_available and s.node.id not in self._drained]

    def drain(self, node_id: str) -> bool:
        """手动摘除节点；返回该节点当前是否存在于池中。"""
        with self._lock:
            found = any(state.node.id == node_id for state in self._states)
            if found:
                self._drained.add(node_id)
        if found:
            self._notify_health()
        return found

    def undrain(self, node_id: str) -> bool:
        """恢复手动摘除的节点；返回此前是否处于摘除状态。"""
        with self._lock:
            if node_id not in self._drained:
                return False
            self._drained.remove(node_id)
        self._notify_health()
        return True

    def state_for(self, node_id: str) -> NodeState | None:
        """按节点 ID 返回当前运行状态。"""
        with self._lock:
            return next((s for s in self._states if s.node.id == node_id), None)

    def acquire(self, exclude: set[str] | None = None) -> NodeState:
        """选择未尝试节点；全不健康时降级尝试，以容忍过期探活结果。"""
        exclude = exclude or set()
        with self._lock:
            states = list(self._states)
            drained = set(self._drained)
        candidates = [s for s in states if s.is_available and s.node.id not in exclude and s.node.id not in drained]

        if not candidates:
            # 手动摘除是运维强约束，不能像过期探活结果那样降级绕过。
            fallback = [s for s in states if s.node.id not in exclude and s.node.id not in drained]
            if not fallback:
                raise TransportError(
                    f"集群中没有可用节点（共 {len(states)} 个，已全部尝试）。"
                    f"各节点状态: {[(s.node.id, s.status.value) for s in states]}"
                )
            log.warning("所有节点均标记为不健康，仍尝试派发——探活结果可能已过时")
            candidates = fallback

        return self.balancer.pick(candidates)

    def snapshot(self) -> dict[str, Any]:
        """生成节点池及所有节点的诊断快照。"""
        with self._lock:
            current = list(self._states)
            drained = set(self._drained)
        states = []
        for state in current:
            item = state.snapshot()
            item["drained"] = state.node.id in drained
            states.append(item)
        return {
            "strategy": self.balancer.name,
            "probe_interval": self.config.probe_interval,
            "failure_threshold": self.config.failure_threshold,
            "recovery_threshold": self.config.recovery_threshold,
            "max_failover": self.config.max_failover,
            "total": len(states),
            "healthy": sum(1 for s in states if s["status"] == NodeStatus.HEALTHY.value),
            "unhealthy": sum(1 for s in states if s["status"] == NodeStatus.UNHEALTHY.value),
            "unknown": sum(1 for s in states if s["status"] == NodeStatus.UNKNOWN.value),
            "nodes": states,
        }

    def __len__(self) -> int:
        with self._lock:
            return len(self._states)

    def __enter__(self) -> NodePool:
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.stop()


__all__ = ["NodePool"]
