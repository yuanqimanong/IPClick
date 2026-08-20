"""静态列表和 DNS 两种集群节点发现实现。"""

from __future__ import annotations

from dataclasses import dataclass
import socket
from typing import Any, Protocol

from ipclick.cluster.node import Node
from ipclick.exceptions import ConfigError
from ipclick.ports import DEFAULT_GRPC_PORT
from ipclick.utils.coerce import as_float, as_int, as_text
from ipclick.utils.log_util import log


DISCOVERY_MODES: frozenset[str] = frozenset({"static", "dns"})


class Discovery(Protocol):
    """节点发现器协议。"""

    name: str

    def resolve(self) -> tuple[Node, ...]:
        """解析并返回当前可用的节点集合。"""
        ...


@dataclass(frozen=True)
class DiscoveryConfig:
    """节点发现方式及刷新频率配置。"""

    mode: str = "static"
    dns_name: str = ""
    port: int = DEFAULT_GRPC_PORT
    refresh_interval: float = 30.0

    @classmethod
    def from_config(cls, cluster_config: dict[str, Any] | None) -> DiscoveryConfig:
        """从集群配置解析并校验发现参数。"""
        config = dict((cluster_config or {}).get("discovery") or {})
        defaults = cls()

        mode = as_text(config.get("mode"), defaults.mode).lower()
        if mode not in DISCOVERY_MODES:
            raise ConfigError(f"未知的节点发现方式 {mode!r}，可选：{'、'.join(sorted(DISCOVERY_MODES))}")

        dns_name = as_text(config.get("dns_name"))
        if mode == "dns" and not dns_name:
            raise ConfigError('[CLUSTER.discovery].mode = "dns" 时必须配置 dns_name')

        return cls(
            mode=mode,
            dns_name=dns_name,
            port=as_int(config.get("port"), defaults.port, minimum=1),
            refresh_interval=as_float(config.get("refresh_interval"), defaults.refresh_interval, minimum=0.0),
        )


class StaticDiscovery:
    """始终返回配置文件中固定节点列表的发现器。"""

    name: str = "static"

    def __init__(self, nodes: tuple[Node, ...]):
        self._nodes: tuple[Node, ...] = nodes

    def resolve(self) -> tuple[Node, ...]:
        """返回构造时提供的静态节点。"""
        return self._nodes


class DnsDiscovery:
    """通过 A/AAAA 记录发现节点，并在解析失败时使用最近缓存。"""

    name: str = "dns"

    def __init__(self, config: DiscoveryConfig, resolver: Any = None):
        self._config: DiscoveryConfig = config
        self._resolver: Any = resolver or socket.getaddrinfo
        self._last: tuple[Node, ...] = ()

    def resolve(self) -> tuple[Node, ...]:
        """解析域名、去重地址并生成稳定排序的节点列表。"""
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
    """根据 ``[CLUSTER.discovery]`` 构造对应发现器。"""
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
