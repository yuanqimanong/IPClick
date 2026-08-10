"""集群支持：多节点负载均衡、健康探测与故障转移。

接通配置文件的 ``[CLUSTER]`` 节。节点探活复用 ``grpc.health.v1``
（见 :mod:`ipclick.health`）。

::

    from ipclick.cluster import ClusterDownloader

    with ClusterDownloader() as d:
        resp = d.get("https://example.com")
        print(d.snapshot())     # 各节点健康状态
"""

from ipclick.cluster.balancer import (
    LoadBalancer,
    RandomBalancer,
    RoundRobinBalancer,
    WeightedBalancer,
    create_balancer,
)
from ipclick.cluster.client import ClusterDownloader
from ipclick.cluster.node import ClusterConfig, Node, NodeState, NodeStatus
from ipclick.cluster.pool import NodePool
from ipclick.cluster.status_page import StatusPageServer, render_page


__all__ = [
    "ClusterConfig",
    "ClusterDownloader",
    "LoadBalancer",
    "Node",
    "NodePool",
    "NodeState",
    "NodeStatus",
    "RandomBalancer",
    "RoundRobinBalancer",
    "StatusPageServer",
    "WeightedBalancer",
    "create_balancer",
    "render_page",
]
