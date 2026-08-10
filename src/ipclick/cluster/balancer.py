"""负载均衡策略。

三种策略都只在**可用节点**里选——不可用节点由 :class:`~ipclick.cluster.pool.NodePool`
的健康探测摘除，策略本身不关心健康判定。
"""

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
    """从候选节点里挑一个。"""

    name: str = "base"

    @abstractmethod
    def pick(self, candidates: Sequence[NodeState]) -> NodeState:
        """选一个节点。candidates 保证非空。"""
        raise NotImplementedError


class RoundRobinBalancer(LoadBalancer):
    """轮询。

    计数器全局递增再对候选数取模，而不是维护"下一个索引"——
    后者在候选集合因健康变化而增删时会跳过或重复某些节点。
    """

    name: str = "round_robin"

    def __init__(self) -> None:
        self._counter: itertools.count[int] = itertools.count()
        self._lock: threading.Lock = threading.Lock()

    @override
    def pick(self, candidates: Sequence[NodeState]) -> NodeState:
        with self._lock:
            index = next(self._counter)
        return candidates[index % len(candidates)]


class RandomBalancer(LoadBalancer):
    """随机。节点多时分布足够均匀，且天然不需要共享状态。"""

    name: str = "random"

    @override
    def pick(self, candidates: Sequence[NodeState]) -> NodeState:
        return random.choice(candidates)


class WeightedBalancer(LoadBalancer):
    """按权重随机。

    用带权随机而不是平滑加权轮询（SWRR）：后者要维护每个节点的当前权重，
    而候选集合随健康状态动态变化，维护那份状态容易出错。带权随机在请求量
    上来之后分布同样收敛到权重比例。
    """

    name: str = "weight"

    @override
    def pick(self, candidates: Sequence[NodeState]) -> NodeState:
        weights = [max(1, state.node.weight) for state in candidates]
        return random.choices(candidates, weights=weights, k=1)[0]


_BALANCERS: dict[str, type[LoadBalancer]] = {
    RoundRobinBalancer.name: RoundRobinBalancer,
    RandomBalancer.name: RandomBalancer,
    WeightedBalancer.name: WeightedBalancer,
    # 常见别名
    "weighted": WeightedBalancer,
    "rr": RoundRobinBalancer,
}


def create_balancer(strategy: str) -> LoadBalancer:
    """按名称创建均衡器。

    Raises:
        ConfigError: 策略名未知。不静默回退到轮询——配置写错了应该让人知道，
            否则权重配置形同虚设而无人察觉。
    """
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
