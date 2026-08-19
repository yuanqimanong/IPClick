"""集群节点负载均衡策略。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
import itertools
import random
import threading

from typing_extensions import override

from ipclick.cluster.node import NodeState
from ipclick.exceptions import ConfigError


class LoadBalancer(ABC):
    """负载均衡器统一接口。"""

    name: str = "base"

    @abstractmethod
    def pick(self, candidates: Sequence[NodeState]) -> NodeState:
        """从非空候选序列中选择一个节点。"""
        raise NotImplementedError


class RoundRobinBalancer(LoadBalancer):
    """以线程安全的递增序号轮询候选节点。"""

    name: str = "round_robin"

    def __init__(self) -> None:
        self._counter: itertools.count[int] = itertools.count()
        self._lock: threading.Lock = threading.Lock()

    @override
    def pick(self, candidates: Sequence[NodeState]) -> NodeState:
        """按全局递增序号轮流选择候选节点。"""
        with self._lock:
            index = next(self._counter)
        return candidates[index % len(candidates)]


class RandomBalancer(LoadBalancer):
    """等概率随机选择候选节点。"""

    name: str = "random"

    @override
    def pick(self, candidates: Sequence[NodeState]) -> NodeState:
        """等概率随机选择一个候选节点。"""
        return random.choice(candidates)


class WeightedBalancer(LoadBalancer):
    """按节点配置权重随机选择候选节点。"""

    name: str = "weight"

    @override
    def pick(self, candidates: Sequence[NodeState]) -> NodeState:
        """按节点权重随机选择一个候选节点。"""
        weights = [max(1, state.node.weight) for state in candidates]
        return random.choices(candidates, weights=weights, k=1)[0]


_BALANCERS: dict[str, type[LoadBalancer]] = {
    RoundRobinBalancer.name: RoundRobinBalancer,
    RandomBalancer.name: RandomBalancer,
    WeightedBalancer.name: WeightedBalancer,
    "weighted": WeightedBalancer,
    "rr": RoundRobinBalancer,
}


def create_balancer(strategy: str) -> LoadBalancer:
    """按配置名称创建负载均衡器，不支持的名称会显式报错。"""
    balancer_class = _BALANCERS.get(strategy.strip().lower())
    if balancer_class is None:
        supported = ", ".join(sorted({b.name for b in _BALANCERS.values()}))
        raise ConfigError(f"未知的负载均衡策略 {strategy!r}，可选: {supported}")
    return balancer_class()


__all__ = [
    "LoadBalancer",
    "RandomBalancer",
    "RoundRobinBalancer",
    "WeightedBalancer",
    "create_balancer",
]
