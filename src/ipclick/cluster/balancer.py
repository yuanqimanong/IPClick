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
    name: str = "base"

    @abstractmethod
    def pick(self, candidates: Sequence[NodeState]) -> NodeState:
        raise NotImplementedError


class RoundRobinBalancer(LoadBalancer):
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
    name: str = "random"

    @override
    def pick(self, candidates: Sequence[NodeState]) -> NodeState:
        return random.choice(candidates)


class WeightedBalancer(LoadBalancer):
    name: str = "weight"

    @override
    def pick(self, candidates: Sequence[NodeState]) -> NodeState:
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
