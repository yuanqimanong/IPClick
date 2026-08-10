"""集群节点发现（静态 / DNS）。

DNS 解析用注入的假 resolver——真去解析域名会让测试依赖网络和 DNS 缓存。
"""

import socket

import pytest

from ipclick.cluster.discovery import (
    DiscoveryConfig,
    DnsDiscovery,
    StaticDiscovery,
    create_discovery,
)
from ipclick.cluster.node import ClusterConfig, Node
from ipclick.cluster.pool import NodePool
from ipclick.exceptions import ConfigError


def _addrinfo(*hosts: str):
    """伪造 socket.getaddrinfo 的返回结构。"""

    def resolver(_name: str, port: int, *_args: object):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port)) for host in hosts]

    return resolver


class TestConfig:
    def test_defaults_to_static(self):
        c = DiscoveryConfig.from_config({})
        assert c.mode == "static"
        assert c.refresh_interval == 30.0

    def test_none_config(self):
        assert DiscoveryConfig.from_config(None).mode == "static"

    def test_parses_dns(self):
        c = DiscoveryConfig.from_config(
            {"discovery": {"mode": "dns", "dns_name": "ipclick.svc", "port": 8080, "refresh_interval": 5}}
        )
        assert (c.mode, c.dns_name, c.port, c.refresh_interval) == ("dns", "ipclick.svc", 8080, 5.0)

    def test_unknown_mode_raises(self):
        """静默回退到 static 的话，扩容出来的节点永远不会被用上。"""
        with pytest.raises(ConfigError, match="未知的节点发现方式"):
            DiscoveryConfig.from_config({"discovery": {"mode": "etcd"}})

    def test_dns_without_name_raises(self):
        with pytest.raises(ConfigError, match="dns_name"):
            DiscoveryConfig.from_config({"discovery": {"mode": "dns"}})

    def test_bad_numbers_fall_back(self):
        c = DiscoveryConfig.from_config({"discovery": {"port": "abc", "refresh_interval": -1}})
        assert (c.port, c.refresh_interval) == (9527, 30.0)

    def test_zero_refresh_means_once(self):
        assert DiscoveryConfig.from_config({"discovery": {"refresh_interval": 0}}).refresh_interval == 0.0


class TestStatic:
    def test_returns_configured_nodes(self):
        nodes = (Node("a", "h1", 1), Node("b", "h2", 2))
        assert StaticDiscovery(nodes).resolve() == nodes

    def test_create_discovery_defaults_to_static(self):
        nodes = (Node("a", "h1", 1),)
        discovery, config = create_discovery({}, nodes)
        assert isinstance(discovery, StaticDiscovery)
        assert config.mode == "static"


class TestDns:
    def _discovery(self, *hosts: str, port: int = 9527) -> DnsDiscovery:
        return DnsDiscovery(
            DiscoveryConfig(mode="dns", dns_name="ipclick.svc", port=port),
            resolver=_addrinfo(*hosts),
        )

    def test_resolves_to_nodes(self):
        nodes = self._discovery("10.0.0.1", "10.0.0.2").resolve()
        assert [n.address for n in nodes] == ["10.0.0.1:9527", "10.0.0.2:9527"]

    def test_uses_configured_port(self):
        assert self._discovery("10.0.0.1", port=8080).resolve()[0].port == 8080

    def test_deduplicates(self):
        """一个域名多条记录指向同一地址是常见的，不该变成两个节点。"""
        assert len(self._discovery("10.0.0.1", "10.0.0.1").resolve()) == 1

    def test_order_is_stable(self):
        """DNS 通常会轮转返回顺序。不排序的话每次刷新都像是"节点全变了"。"""
        a = DnsDiscovery(
            DiscoveryConfig(mode="dns", dns_name="x"), resolver=_addrinfo("10.0.0.2", "10.0.0.1")
        ).resolve()
        b = DnsDiscovery(
            DiscoveryConfig(mode="dns", dns_name="x"), resolver=_addrinfo("10.0.0.1", "10.0.0.2")
        ).resolve()
        assert [n.id for n in a] == [n.id for n in b]

    def test_id_is_address_so_state_survives_refresh(self):
        """DNS 里没有稳定的节点标识，用地址做 id 才能在多次解析之间对上同一个节点。"""
        nodes = self._discovery("10.0.0.1").resolve()
        assert nodes[0].id == "10.0.0.1:9527"

    def test_failure_reuses_last_result(self):
        """DNS 抖一下就把整个集群摘空，比暂时用着略微过期的列表危险得多。"""
        calls = {"n": 0}

        def flaky(_name: str, port: int, *_args: object):
            calls["n"] += 1
            if calls["n"] == 1:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", port))]
            raise OSError("temporary failure in name resolution")

        discovery = DnsDiscovery(DiscoveryConfig(mode="dns", dns_name="x"), resolver=flaky)
        first = discovery.resolve()
        assert discovery.resolve() == first, "解析失败时应沿用上一次的结果"

    def test_failure_on_first_resolve_raises(self):
        """启动时就解析不出来是真的配错了，不能带着空集群起来。"""

        def broken(*_args: object):
            raise OSError("nope")

        with pytest.raises(ConfigError, match="解析集群域名"):
            DnsDiscovery(DiscoveryConfig(mode="dns", dns_name="x"), resolver=broken).resolve()

    def test_empty_result_on_first_resolve_raises(self):
        with pytest.raises(ConfigError, match="没有解析出任何地址"):
            DnsDiscovery(DiscoveryConfig(mode="dns", dns_name="x"), resolver=_addrinfo()).resolve()


class TestPoolRefresh:
    """刷新时保住健康状态——这是最容易写错、后果又最隐蔽的一点。"""

    def _pool(self, resolver, interval: float = 0.0) -> NodePool:
        config = ClusterConfig(nodes=(Node("seed", "127.0.0.1", 1),), probe_interval=999)
        discovery = DnsDiscovery(
            DiscoveryConfig(mode="dns", dns_name="x", refresh_interval=interval), resolver=resolver
        )
        return NodePool(
            config,
            start_probing=False,
            discovery=discovery,
            discovery_config=DiscoveryConfig(mode="dns", dns_name="x", refresh_interval=interval),
        )

    def test_initial_nodes_come_from_discovery(self):
        pool = self._pool(_addrinfo("10.0.0.1", "10.0.0.2"))
        assert len(pool) == 2

    def test_adds_and_removes(self):
        hosts = ["10.0.0.1", "10.0.0.2"]

        def resolver(_name: str, port: int, *_args: object):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (h, port)) for h in hosts]

        pool = self._pool(resolver)
        assert len(pool) == 2
        hosts.append("10.0.0.3")
        assert pool.refresh_nodes(force=True) is True
        assert len(pool) == 3
        hosts.remove("10.0.0.1")
        assert pool.refresh_nodes(force=True) is True
        assert len(pool) == 2

    def test_unchanged_list_reports_no_change(self):
        pool = self._pool(_addrinfo("10.0.0.1"))
        assert pool.refresh_nodes(force=True) is False

    def test_health_state_survives_refresh(self):
        """回归：刷新时重建节点列表会把健康计数清零。

        一个每 30 秒刷新一次的池子，任何"连续 N 次"的判定都永远达不到——
        熔断和恢复双双失效，而表面上一切正常。
        """
        hosts = ["10.0.0.1"]

        def resolver(_name: str, port: int, *_args: object):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (h, port)) for h in hosts]

        pool = self._pool(resolver)
        state = pool.snapshot()["nodes"][0]
        assert state["total_requests"] == 0

        # 制造一些历史
        for s in pool._states:
            s.record_request(success=True)
            s.record_request(success=False)
            s.mark_unhealthy("boom")

        hosts.append("10.0.0.2")  # 扩容触发刷新
        pool.refresh_nodes(force=True)

        kept = next(n for n in pool.snapshot()["nodes"] if n["id"] == "10.0.0.1:9527")
        assert kept["total_requests"] == 2, "刷新把已有节点的统计清零了"
        assert kept["status"] == "unhealthy", "刷新把已有节点的健康状态清零了"

    def test_refresh_respects_interval(self):
        calls = {"n": 0}

        def counting(_name: str, port: int, *_args: object):
            calls["n"] += 1
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", port))]

        pool = self._pool(counting, interval=999)
        before = calls["n"]
        pool.refresh_nodes()  # 间隔没到，应当直接返回
        assert calls["n"] == before

    def test_resolve_failure_keeps_current_nodes(self):
        state = {"fail": False}

        def flaky(_name: str, port: int, *_args: object):
            if state["fail"]:
                raise OSError("dns down")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", port))]

        pool = self._pool(flaky)
        state["fail"] = True
        assert pool.refresh_nodes(force=True) is False
        assert len(pool) == 1, "DNS 挂了不能把集群摘空"
