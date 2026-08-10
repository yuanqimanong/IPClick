"""节点池：维护节点状态、后台探活、按策略选节点。

探活复用 P2-2 实现的 ``grpc.health.v1`` —— 这也是当初把健康检查排在集群
之前做的原因。
"""

from __future__ import annotations

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
    """一组 IPClick 服务端节点。

    线程安全：请求线程调 :meth:`acquire`，后台探活线程调 :meth:`probe_once`。
    """

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

        # 节点来源：静态配置或 DNS 发现
        self._discovery: Discovery = discovery or StaticDiscovery(config.nodes)
        self._discovery_config: DiscoveryConfig = discovery_config or DiscoveryConfig()
        # 构造时已经解析过一轮了，从现在开始计间隔。
        # 初值给 0 的话 now - 0 必然大于任何间隔，第一次调用就会白白重解析一次。
        self._last_refresh: float = time.monotonic()

        initial = self._discovery.resolve()
        if not initial:
            raise ConfigError("集群模式需要至少一个节点（[CLUSTER].nodes 或 [CLUSTER.discovery]）")
        self._states: list[NodeState] = [NodeState(node) for node in initial]

        # 探活也要走 TLS：服务端开了 TLS 而探活还用明文，会把健康节点全判成挂了
        self._tls: TLSSettings = tls or TLSSettings()

        self._stop: threading.Event = threading.Event()
        self._thread: threading.Thread | None = None
        if start_probing:
            self.start()

    # ---------------------------------------------------------------- #
    # 探活线程
    # ---------------------------------------------------------------- #

    def start(self) -> None:
        """启动后台探活线程。可重复调用。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._probe_loop, name="ipclick-health-probe", daemon=True)
        self._thread.start()
        log.debug(f"集群探活已启动，{len(self._states)} 个节点，间隔 {self.config.probe_interval}s")

    def stop(self) -> None:
        """停止后台探活线程。"""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=self.config.probe_interval + 2)
        self._thread = None

    def _probe_loop(self) -> None:
        # 先立刻探一轮，别让首批请求撞在"全 UNKNOWN"的状态上
        self.probe_once()
        while not self._stop.wait(self.config.probe_interval):
            self.refresh_nodes()
            self.probe_once()

    # ---------------------------------------------------------------- #
    # 节点发现
    # ---------------------------------------------------------------- #

    def refresh_nodes(self, *, force: bool = False) -> bool:
        """重新解析节点列表。返回节点集合是否有变化。

        按 id 复用已有的 NodeState——直接重建会把每个节点的健康计数与请求统计
        清零，那样任何"连续 N 次"的判定都永远达不到，熔断和恢复双双失效。
        """
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
            refreshed = [existing.get(node.id) or NodeState(node) for node in nodes]
            added = [n.id for n in nodes if n.id not in existing]
            removed = [i for i in existing if i not in {n.id for n in nodes}]
            if not added and not removed:
                return False
            self._states = refreshed

        if added:
            log.info(f"集群新增节点：{', '.join(added)}")
        if removed:
            log.info(f"集群移除节点：{', '.join(removed)}")
        return True

    def probe_once(self) -> None:
        """对所有节点探一次活。

        串行探测：节点数通常是个位数，为此再开一个线程池不划算，而且探活本身
        有超时保护。真到几十个节点再改成并发。
        """
        for state in self._states:
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

    # ---------------------------------------------------------------- #
    # 选节点
    # ---------------------------------------------------------------- #

    def available(self) -> list[NodeState]:
        with self._lock:
            return [s for s in self._states if s.is_available]

    def acquire(self, exclude: set[str] | None = None) -> NodeState:
        """选一个可用节点。

        Args:
            exclude: 本次请求已经试过、要跳过的节点 id（故障转移时用）。

        Raises:
            TransportError: 没有可用节点。
        """
        exclude = exclude or set()
        candidates = [s for s in self.available() if s.node.id not in exclude]

        if not candidates:
            # 全都不可用时，退而求其次：在未被排除的节点里随便挑一个试试。
            # 完全拒绝服务不如赌一把——探活结果可能已经过时了。
            fallback = [s for s in self._states if s.node.id not in exclude]
            if not fallback:
                raise TransportError(
                    f"集群中没有可用节点（共 {len(self._states)} 个，已全部尝试）。"
                    f"各节点状态: {[(s.node.id, s.status.value) for s in self._states]}"
                )
            log.warning("所有节点均标记为不健康，仍尝试派发——探活结果可能已过时")
            candidates = fallback

        return self.balancer.pick(candidates)

    # ---------------------------------------------------------------- #
    # 观测
    # ---------------------------------------------------------------- #

    def snapshot(self) -> dict[str, Any]:
        """给状态页用的整体快照。"""
        states = [s.snapshot() for s in self._states]
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
        return len(self._states)

    def __enter__(self) -> NodePool:
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.stop()


__all__ = ["NodePool"]
