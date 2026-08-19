"""集群节点发现。

原来节点列表只能写死在 ``[CLUSTER].nodes`` 里，扩缩容要改配置再重启每一个
客户端。这里加上 DNS 发现：给一个域名，解析出的每个 A/AAAA 记录就是一个节点，
后台定期重解析。K8s 的 headless Service、Consul 的 DNS 接口、云厂商的内网
负载均衡域名都能直接用。

保留健康状态
------------
刷新时**不能**简单地重建节点列表——那会把每个节点的健康计数、请求统计全部清零。
一个每 30 秒刷新一次的池子，任何需要连续 2 次探测才能判定的状态都永远达不到，
熔断和恢复双双失效。所以这里按节点 id 复用已有的 :class:`NodeState`，
只增删真正变化的那部分。
"""

from __future__ import annotations

from dataclasses import dataclass
import socket
from typing import Any, Protocol

from ipclick.cluster.node import Node
from ipclick.exceptions import ConfigError
from ipclick.ports import DEFAULT_GRPC_PORT
from ipclick.utils.log_util import log


DISCOVERY_MODES: frozenset[str] = frozenset({"static", "dns"})


class Discovery(Protocol):
    """解析出当前的节点列表。"""

    name: str

    def resolve(self) -> tuple[Node, ...]: ...


@dataclass(frozen=True)
class DiscoveryConfig:
    """来自 ``[CLUSTER].discovery``。"""

    mode: str = "static"
    dns_name: str = ""
    port: int = DEFAULT_GRPC_PORT
    refresh_interval: float = 30.0

    @classmethod
    def from_config(cls, cluster_config: dict[str, Any] | None) -> DiscoveryConfig:
        config = dict((cluster_config or {}).get("discovery") or {})
        defaults = cls()

        mode = str(config.get("mode") or defaults.mode).strip().lower()
        if mode not in DISCOVERY_MODES:
            raise ConfigError(f"未知的节点发现方式 {mode!r}，可选：{'、'.join(sorted(DISCOVERY_MODES))}")

        def _num(key: str, fallback: float) -> float:
            try:
                value = float(config.get(key))  # pyright: ignore[reportArgumentType]
            except (TypeError, ValueError):
                return fallback
            return value if value >= 0 else fallback

        dns_name = str(config.get("dns_name") or "").strip()
        if mode == "dns" and not dns_name:
            raise ConfigError('[CLUSTER.discovery].mode = "dns" 时必须配置 dns_name')

        return cls(
            mode=mode,
            dns_name=dns_name,
            port=int(_num("port", defaults.port)) or defaults.port,
            refresh_interval=_num("refresh_interval", defaults.refresh_interval),
        )


class StaticDiscovery:
    """节点来自配置文件，永不变化。"""

    name: str = "static"

    def __init__(self, nodes: tuple[Node, ...]):
        self._nodes: tuple[Node, ...] = nodes

    def resolve(self) -> tuple[Node, ...]:
        return self._nodes


class DnsDiscovery:
    """把一个域名解析成节点列表。

    解析失败时**返回上一次的结果**而不是空列表：DNS 抖一下就把整个集群摘空，
    比暂时用着略微过期的列表危险得多。
    """

    name: str = "dns"

    def __init__(self, config: DiscoveryConfig, resolver: Any = None):
        self._config: DiscoveryConfig = config
        self._resolver: Any = resolver or socket.getaddrinfo
        self._last: tuple[Node, ...] = ()

    def resolve(self) -> tuple[Node, ...]:
        name = self._config.dns_name
        port = self._config.port
        try:
            records = self._resolver(name, port, 0, socket.SOCK_STREAM)
        except OSError as e:
            if self._last:
                log.warning(f"解析 {name} 失败（{e}），沿用上一次的 {len(self._last)} 个节点")
                return self._last
            raise ConfigError(f"解析集群域名 {name} 失败：{e}") from e

        addresses: list[str] = []
        for record in records:
            sockaddr = record[4]
            host = str(sockaddr[0])
            if host not in addresses:
                addresses.append(host)

        if not addresses:
            if self._last:
                log.warning(f"{name} 解析结果为空，沿用上一次的 {len(self._last)} 个节点")
                return self._last
            raise ConfigError(f"集群域名 {name} 没有解析出任何地址")

        nodes = tuple(Node(id=f"{host}:{port}", host=host, port=port) for host in sorted(addresses))
        if nodes != self._last:
            log.info(f"{name} 解析到 {len(nodes)} 个节点：{', '.join(n.address for n in nodes)}")
        self._last = nodes
        return nodes


def create_discovery(
    cluster_config: dict[str, Any] | None,
    static_nodes: tuple[Node, ...],
    *,
    resolver: Any = None,
) -> tuple[Discovery, DiscoveryConfig]:
    """按配置造出发现器。"""
    config = DiscoveryConfig.from_config(cluster_config)
    if config.mode == "dns":
        return DnsDiscovery(config, resolver=resolver), config
    return StaticDiscovery(static_nodes), config


__all__ = [
    "DISCOVERY_MODES",
    "Discovery",
    "DiscoveryConfig",
    "DnsDiscovery",
    "StaticDiscovery",
    "create_discovery",
]
