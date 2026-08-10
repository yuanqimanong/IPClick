"""集群：节点模型、负载均衡、健康探测、故障转移、状态页。"""

from collections.abc import Iterator
from concurrent import futures
from typing import Any

import grpc
import pytest

from ipclick.cluster import (
    ClusterConfig,
    ClusterDownloader,
    Node,
    NodePool,
    NodeState,
    NodeStatus,
    RandomBalancer,
    RoundRobinBalancer,
    WeightedBalancer,
    create_balancer,
    render_page,
)
from ipclick.cluster.status_page import StatusPageServer
from ipclick.dto.proto import task_pb2_grpc
from ipclick.exceptions import ConfigError, TransportError, ValidationError
from ipclick.health import HealthReporter
from ipclick.services.task_service import TaskService
from ipclick.utils.config_util import Settings
from tests.test_sdk_e2e import EchoAdapter, _free_port


# ---------------------------------------------------------------------- #
# 节点模型
# ---------------------------------------------------------------------- #


class TestNode:
    def test_from_config_minimal(self):
        n = Node.from_config({"address": "10.0.0.1:9527"})
        assert (n.host, n.port, n.weight) == ("10.0.0.1", 9527, 100)
        assert n.id == "10.0.0.1:9527", "未给 id 时用 address 兜底"

    def test_from_config_full(self):
        n = Node.from_config(
            {"id": "n1", "address": "h:1", "weight": 50, "region": "cn", "zone": "a", "tags": ["x", "y"]}
        )
        assert (n.id, n.weight, n.region, n.zone, n.tags) == ("n1", 50, "cn", "a", ("x", "y"))

    def test_ipv6_literal(self):
        n = Node.from_config({"address": "[::1]:9527"})
        assert (n.host, n.port) == ("::1", 9527)

    def test_missing_address_rejected(self):
        with pytest.raises(ConfigError, match="缺少 address"):
            Node.from_config({"id": "x"})

    def test_bad_address_rejected(self):
        with pytest.raises(ConfigError, match="host:port"):
            Node.from_config({"address": "no-port"})

    def test_non_numeric_port_rejected(self):
        with pytest.raises(ConfigError, match="端口不是数字"):
            Node.from_config({"address": "h:abc"})

    def test_bad_weight_falls_back(self):
        assert Node.from_config({"address": "h:1", "weight": "abc"}).weight == 100
        assert Node.from_config({"address": "h:1", "weight": 0}).weight == 1


class TestNodeState:
    def _state(self) -> NodeState:
        return NodeState(Node(id="n", host="h", port=1))

    def test_starts_unknown_but_available(self):
        """冷启动时全部 UNKNOWN，若视为不可用则首批请求会全军覆没。"""
        s = self._state()
        assert s.status is NodeStatus.UNKNOWN
        assert s.is_available

    def test_needs_threshold_failures_to_go_unhealthy(self):
        """单次抖动就摘节点会导致流量反复横跳。"""
        s = self._state()
        s.record_probe(False, "boom", failure_threshold=2, recovery_threshold=2)
        assert s.status is NodeStatus.UNKNOWN, "一次失败不应立刻摘除"
        s.record_probe(False, "boom", failure_threshold=2, recovery_threshold=2)
        assert s.status is NodeStatus.UNHEALTHY
        assert not s.is_available

    def test_needs_threshold_successes_to_recover(self):
        s = self._state()
        for _ in range(2):
            s.record_probe(False, "boom", failure_threshold=2, recovery_threshold=2)
        s.record_probe(True, "SERVING", failure_threshold=2, recovery_threshold=2)
        assert s.status is NodeStatus.UNHEALTHY, "一次成功不应立刻恢复"
        s.record_probe(True, "SERVING", failure_threshold=2, recovery_threshold=2)
        assert s.status is NodeStatus.HEALTHY

    def test_interleaved_results_reset_counters(self):
        """成功会清零失败计数，反之亦然——否则累计计数迟早会跨越阈值。"""
        s = self._state()
        s.record_probe(False, "e", failure_threshold=3, recovery_threshold=1)
        s.record_probe(True, "ok", failure_threshold=3, recovery_threshold=1)
        s.record_probe(False, "e", failure_threshold=3, recovery_threshold=1)
        s.record_probe(False, "e", failure_threshold=3, recovery_threshold=1)
        assert s.status is NodeStatus.HEALTHY, "失败计数应已被中间那次成功清零"

    def test_change_flag(self):
        s = self._state()
        assert not s.record_probe(False, "e", failure_threshold=2, recovery_threshold=1)
        assert s.record_probe(False, "e", failure_threshold=2, recovery_threshold=1)

    def test_mark_unhealthy_is_immediate(self):
        s = self._state()
        s.mark_unhealthy("请求失败")
        assert s.status is NodeStatus.UNHEALTHY

    def test_snapshot_fields(self):
        s = self._state()
        s.record_request(success=True)
        s.record_request(success=False)
        snap = s.snapshot()
        assert snap["total_requests"] == 2
        assert snap["total_failures"] == 1
        assert snap["status"] == "unknown"


# ---------------------------------------------------------------------- #
# 负载均衡
# ---------------------------------------------------------------------- #


def _states(count: int, weights: list[int] | None = None) -> list[NodeState]:
    return [
        NodeState(Node(id=f"n{i}", host="h", port=i, weight=(weights[i] if weights else 100))) for i in range(count)
    ]


class TestBalancers:
    def test_round_robin_cycles(self):
        pool = _states(3)
        b = RoundRobinBalancer()
        picked = [b.pick(pool).node.id for _ in range(6)]
        assert picked == ["n0", "n1", "n2", "n0", "n1", "n2"]

    def test_round_robin_survives_shrinking_pool(self):
        """候选集合会随健康状态变化，均衡器不能因此崩溃或死锁。"""
        b = RoundRobinBalancer()
        big, small = _states(3), _states(1)
        for _ in range(5):
            assert b.pick(big) is not None
            assert b.pick(small) is not None

    def test_random_covers_all(self):
        pool = _states(3)
        b = RandomBalancer()
        seen = {b.pick(pool).node.id for _ in range(200)}
        assert seen == {"n0", "n1", "n2"}

    def test_weighted_respects_weights(self):
        """权重 90:10 时，高权重节点应拿到明显更多流量。"""
        pool = _states(2, weights=[90, 10])
        b = WeightedBalancer()
        counts = {"n0": 0, "n1": 0}
        for _ in range(2000):
            counts[b.pick(pool).node.id] += 1
        assert counts["n0"] > counts["n1"] * 3, f"权重未生效: {counts}"

    def test_create_by_name(self):
        assert create_balancer("round_robin").name == "round_robin"
        assert create_balancer("RANDOM").name == "random"
        assert create_balancer("weighted").name == "weight"

    def test_unknown_strategy_raises(self):
        """配置写错了应该让人知道，静默回退会让权重配置形同虚设。"""
        with pytest.raises(ConfigError, match="未知的负载均衡策略"):
            create_balancer("magic")


# ---------------------------------------------------------------------- #
# 配置
# ---------------------------------------------------------------------- #


class TestClusterConfig:
    def test_empty_means_disabled(self):
        assert not ClusterConfig.from_config({}).enabled
        assert not ClusterConfig.from_config(None).enabled

    def test_parses_nodes_and_options(self):
        c = ClusterConfig.from_config(
            {
                "load_balancer": "weight",
                "failure_threshold": 5,
                "probe_interval": 30,
                "max_failover": 4,
                "nodes": [{"id": "a", "address": "h1:1"}, {"id": "b", "address": "h2:2"}],
            }
        )
        assert c.enabled
        assert len(c.nodes) == 2
        assert c.strategy == "weight"
        assert c.failure_threshold == 5
        assert c.probe_interval == 30.0
        assert c.max_failover == 4

    def test_bad_values_fall_back(self):
        c = ClusterConfig.from_config({"probe_interval": "abc", "failure_threshold": 0, "nodes": []})
        assert c.probe_interval == ClusterConfig().probe_interval
        assert c.failure_threshold == ClusterConfig().failure_threshold

    def test_shipped_default_config_parses(self):
        from ipclick.config_loader.loader import load_config

        load_config.cache_clear()
        c = ClusterConfig.from_config(dict(load_config().get("CLUSTER", {})))
        assert not c.enabled, "随包默认配置不应预置任何节点"


# ---------------------------------------------------------------------- #
# 节点池
# ---------------------------------------------------------------------- #


class TestNodePool:
    def test_requires_nodes(self):
        with pytest.raises(ConfigError, match="至少一个"):
            NodePool(ClusterConfig(), start_probing=False)

    def test_acquire_skips_unhealthy(self):
        config = ClusterConfig(nodes=(Node("a", "h", 1), Node("b", "h", 2)), strategy="round_robin")
        pool = NodePool(config, start_probing=False)
        pool._states[0].mark_unhealthy("down")
        assert {pool.acquire().node.id for _ in range(10)} == {"b"}

    def test_acquire_honours_exclude(self):
        config = ClusterConfig(nodes=(Node("a", "h", 1), Node("b", "h", 2)))
        pool = NodePool(config, start_probing=False)
        assert pool.acquire(exclude={"a"}).node.id == "b"

    def test_all_unhealthy_still_tries(self):
        """全挂时赌一把好过直接拒服务——探活结果可能已经过时了。"""
        config = ClusterConfig(nodes=(Node("a", "h", 1),))
        pool = NodePool(config, start_probing=False)
        pool._states[0].mark_unhealthy("down")
        assert pool.acquire().node.id == "a"

    def test_raises_when_everything_excluded(self):
        config = ClusterConfig(nodes=(Node("a", "h", 1),))
        pool = NodePool(config, start_probing=False)
        with pytest.raises(TransportError, match="没有可用节点"):
            pool.acquire(exclude={"a"})

    def test_snapshot_counts(self):
        config = ClusterConfig(nodes=(Node("a", "h", 1), Node("b", "h", 2), Node("c", "h", 3)))
        pool = NodePool(config, start_probing=False)
        pool._states[0].mark_unhealthy("down")
        snap = pool.snapshot()
        assert snap["total"] == 3
        assert snap["unhealthy"] == 1
        assert snap["unknown"] == 2

    def test_probe_marks_dead_node(self):
        """探活对着一个没人监听的端口，应当把节点摘掉。"""
        dead = _free_port()
        config = ClusterConfig(
            nodes=(Node("dead", "127.0.0.1", dead),),
            failure_threshold=1,
            probe_timeout=1.0,
        )
        pool = NodePool(config, start_probing=False)
        pool.probe_once()
        assert pool._states[0].status is NodeStatus.UNHEALTHY

    def test_probe_marks_live_node_healthy(self, monkeypatch: pytest.MonkeyPatch):
        """对着一个真实的、注册了健康检查的服务端探活。"""
        port, server, service = _start_node(monkeypatch)
        try:
            config = ClusterConfig(
                nodes=(Node("live", "127.0.0.1", port),),
                recovery_threshold=1,
                probe_timeout=3.0,
            )
            pool = NodePool(config, start_probing=False)
            pool.probe_once()
            assert pool._states[0].status is NodeStatus.HEALTHY
        finally:
            server.stop(grace=0).wait(timeout=5)
            service.cleanup()


def _start_node(monkeypatch: pytest.MonkeyPatch) -> tuple[int, Any, TaskService]:
    """起一个带健康检查的真实 IPClick 服务端。"""
    adapter = EchoAdapter()
    monkeypatch.setattr("ipclick.services.task_service.get_default_adapter", lambda settings=None: adapter)
    monkeypatch.setattr(
        "ipclick.services.task_service.get_adapter", lambda name, settings=None, browser_settings=None: adapter
    )
    service = TaskService(Settings({}))
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    task_pb2_grpc.add_TaskServiceServicer_to_server(service, server)
    reporter = HealthReporter(enabled=True)
    reporter.register(server)
    port = _free_port()
    server.add_insecure_port(f"127.0.0.1:{port}")
    server.start()
    reporter.set_serving()
    return port, server, service


# ---------------------------------------------------------------------- #
# 集群客户端与故障转移
# ---------------------------------------------------------------------- #


@pytest.fixture
def two_nodes(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[int, int, list[Any]]]:
    """起两个真实节点。"""
    p1, s1, svc1 = _start_node(monkeypatch)
    p2, s2, svc2 = _start_node(monkeypatch)
    try:
        yield p1, p2, [s1, s2]
    finally:
        for s in (s1, s2):
            s.stop(grace=0).wait(timeout=5)
        for svc in (svc1, svc2):
            svc.cleanup()


class TestClusterDownloader:
    def test_requests_spread_across_nodes(self, two_nodes: tuple[int, int, list[Any]]):
        p1, p2, _ = two_nodes
        config = ClusterConfig(
            nodes=(Node("a", "127.0.0.1", p1), Node("b", "127.0.0.1", p2)),
            strategy="round_robin",
        )
        with ClusterDownloader(cluster_config=config, start_probing=False) as d:
            for _ in range(4):
                assert d.get("http://example.com/x").status_code == 200
            snap = d.snapshot()
        counts = {n["id"]: n["total_requests"] for n in snap["nodes"]}
        assert counts == {"a": 2, "b": 2}, f"轮询未均分: {counts}"

    def test_failover_to_healthy_node(self, monkeypatch: pytest.MonkeyPatch):
        """一个节点是死端口，请求应自动转移到活着的那个。"""
        live_port, server, service = _start_node(monkeypatch)
        dead_port = _free_port()
        try:
            config = ClusterConfig(
                # 死节点排在前面，确保第一次一定打到它
                nodes=(Node("dead", "127.0.0.1", dead_port), Node("live", "127.0.0.1", live_port)),
                strategy="round_robin",
                max_failover=2,
            )
            with ClusterDownloader(cluster_config=config, start_probing=False) as d:
                resp = d.get("http://example.com/x", timeout=3, max_retries=0)
                assert resp.status_code == 200, "未能转移到健康节点"

                snap = {n["id"]: n for n in d.snapshot()["nodes"]}
                assert snap["dead"]["status"] == "unhealthy", "失败的节点应被立刻摘除"
                assert snap["live"]["total_requests"] >= 1
        finally:
            server.stop(grace=0).wait(timeout=5)
            service.cleanup()

    def test_all_nodes_dead_raises(self):
        config = ClusterConfig(
            nodes=(Node("d1", "127.0.0.1", _free_port()), Node("d2", "127.0.0.1", _free_port())),
            max_failover=1,
        )
        with (
            ClusterDownloader(cluster_config=config, start_probing=False) as d,
            pytest.raises(TransportError, match="均失败"),
        ):
            d.download(_task("http://example.com/x"))

    def test_validation_error_not_retried_on_other_nodes(self, two_nodes: tuple[int, int, list[Any]]):
        """参数错误换节点还是一样的结果，不该浪费尝试次数、也不该拖慢反馈。"""
        p1, p2, _ = two_nodes
        config = ClusterConfig(nodes=(Node("a", "127.0.0.1", p1), Node("b", "127.0.0.1", p2)))
        with ClusterDownloader(cluster_config=config, start_probing=False) as d:
            with pytest.raises(ValidationError):
                d.get("http://example.com/x", adapter="htttpx")

            total = sum(n["total_requests"] for n in d.snapshot()["nodes"])
            assert total == 1, f"参数错误被在多个节点上重试了 {total} 次"

    def test_batch_through_cluster(self, two_nodes: tuple[int, int, list[Any]]):
        from ipclick.dto.models import DownloadTask

        p1, p2, _ = two_nodes
        config = ClusterConfig(nodes=(Node("a", "127.0.0.1", p1), Node("b", "127.0.0.1", p2)))
        tasks = [DownloadTask(uuid=f"t{i}", url=f"http://example.com/{i}") for i in range(4)]
        with ClusterDownloader(cluster_config=config, start_probing=False) as d:
            got = {r.request_uuid for r in d.batch(tasks)}
        assert got == {f"t{i}" for i in range(4)}

    def test_stream_through_cluster(self, two_nodes: tuple[int, int, list[Any]]):
        p1, p2, _ = two_nodes
        config = ClusterConfig(nodes=(Node("a", "127.0.0.1", p1), Node("b", "127.0.0.1", p2)))
        with (
            ClusterDownloader(cluster_config=config, start_probing=False) as d,
            d.stream("http://example.com/x") as resp,
        ):
            assert resp.status_code == 200
            assert resp.read()

    def test_close_is_idempotent(self, two_nodes: tuple[int, int, list[Any]]):
        p1, _, _ = two_nodes
        d = ClusterDownloader(cluster_config=ClusterConfig(nodes=(Node("a", "127.0.0.1", p1),)), start_probing=False)
        d.close()
        d.close()


def _task(url: str) -> Any:
    from ipclick.dto.models import DownloadTask

    return DownloadTask(url=url, timeout=3, max_retries=0)


# ---------------------------------------------------------------------- #
# 状态页
# ---------------------------------------------------------------------- #


_SNAPSHOT: dict[str, Any] = {
    "strategy": "round_robin",
    "probe_interval": 10,
    "failure_threshold": 2,
    "recovery_threshold": 2,
    "max_failover": 2,
    "total": 2,
    "healthy": 1,
    "unhealthy": 1,
    "unknown": 0,
    "nodes": [
        {
            "id": "n1",
            "address": "10.0.0.1:9527",
            "status": "healthy",
            "weight": 100,
            "region": "cn",
            "zone": "a",
            "total_requests": 7,
            "total_failures": 0,
            "last_checked_ago": 3.0,
            "last_error": "",
        },
        {
            "id": "n2",
            "address": "10.0.0.2:9527",
            "status": "unhealthy",
            "weight": 50,
            "region": "",
            "zone": "",
            "total_requests": 2,
            "total_failures": 2,
            "last_checked_ago": 120.0,
            "last_error": "connection refused",
        },
    ],
}


class TestStatusPageRendering:
    def test_renders_nodes(self):
        html = render_page(_SNAPSHOT)
        assert "10.0.0.1:9527" in html
        assert "10.0.0.2:9527" in html
        assert "round_robin" in html

    def test_shows_error_for_unhealthy(self):
        assert "connection refused" in render_page(_SNAPSHOT)

    def test_escapes_html(self):
        """节点 id 与错误信息来自配置或远端，直接拼进页面会有注入风险。"""
        evil = dict(_SNAPSHOT)
        evil["nodes"] = [
            {**_SNAPSHOT["nodes"][0], "id": "<script>alert(1)</script>", "last_error": ""},
            {**_SNAPSHOT["nodes"][1], "last_error": "<img src=x onerror=alert(1)>"},
        ]
        html = render_page(evil)
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html
        assert "<img src=x" not in html

    def test_empty_cluster(self):
        html = render_page({**_SNAPSHOT, "nodes": [], "total": 0, "healthy": 0, "unhealthy": 0, "unknown": 0})
        assert "没有配置任何节点" in html


class TestStatusPageServer:
    @pytest.fixture
    def page(self) -> Iterator[int]:
        server = StatusPageServer(lambda: _SNAPSHOT)
        port = _free_port()
        assert server.start(port, "127.0.0.1")
        try:
            yield port
        finally:
            server.stop()

    @staticmethod
    def _fetch(port: int, path: str) -> tuple[int, str]:
        import urllib.error
        import urllib.request

        # 本机页面，别让环境代理把它劫走
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            resp = opener.open(f"http://127.0.0.1:{port}{path}", timeout=5)
            return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_serves_html(self, page: int):
        status, body = self._fetch(page, "/")
        assert status == 200
        assert "IPClick 集群状态" in body
        assert "10.0.0.1:9527" in body

    def test_serves_json(self, page: int):
        import json

        status, body = self._fetch(page, "/api/nodes")
        assert status == 200
        data = json.loads(body)
        assert data["total"] == 2
        assert len(data["nodes"]) == 2

    def test_unknown_path_404(self, page: int):
        assert self._fetch(page, "/nope")[0] == 404

    def test_write_methods_rejected(self, page: int):
        """页面刻意只读——写方法必须显式拒绝，而不是靠"没实现"隐式拒绝。"""
        import urllib.error
        import urllib.request

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            req = urllib.request.Request(f"http://127.0.0.1:{page}/", method=method, data=b"")
            try:
                opener.open(req, timeout=5)
                pytest.fail(f"{method} 未被拒绝")
            except urllib.error.HTTPError as e:
                assert e.code == 405, f"{method} 返回 {e.code}，应为 405"

    def test_no_cache_header(self, page: int):
        import urllib.request

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        resp = opener.open(f"http://127.0.0.1:{page}/", timeout=5)
        assert resp.headers.get("Cache-Control") == "no-store"

    def test_snapshot_failure_returns_503_not_crash(self):
        def boom() -> dict[str, Any]:
            raise RuntimeError("pool gone")

        server = StatusPageServer(boom)
        port = _free_port()
        assert server.start(port, "127.0.0.1")
        try:
            status, body = self._fetch(port, "/")
            assert status == 503
            assert "pool gone" in body
        finally:
            server.stop()

    def test_port_conflict_returns_false(self):
        import socket

        holder = socket.socket()
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        try:
            server = StatusPageServer(lambda: _SNAPSHOT)
            assert server.start(int(holder.getsockname()[1]), "127.0.0.1") is False
        finally:
            holder.close()
