"""0.4 的三件"不用重启"：集群热更新、节点探测、``{port}`` 占位符。

前两件是 P0-1 / P1-1 的核心诉求，第三件是 P1-3。共同点是它们要么改了对象的
生命周期（原本等于进程生命周期），要么改了配置的解析时机——两类改动都容易在
"看起来对、实际没生效"的地方出错，所以断言全部盯着**实际生效的那份状态**，
而不是文件内容。
"""

from __future__ import annotations

from typing import Any

import grpc
import pytest

from ipclick.cluster.node import ClusterConfig, Node, NodeState, NodeStatus
from ipclick.cluster.pool import NodePool
from ipclick.cluster.probe import ProbeResult, _from_rpc_error
from ipclick.config_loader import placeholders
from ipclick.utils.config_util import Settings


def _cluster(*addresses: tuple[str, str], strategy: str = "round_robin") -> ClusterConfig:
    return ClusterConfig(
        nodes=tuple(Node.from_config({"id": node_id, "address": address}) for node_id, address in addresses),
        strategy=strategy,
    )


# --------------------------------------------------------------------------- #
# 节点池热更新
# --------------------------------------------------------------------------- #


class TestPoolReplace:
    @pytest.fixture
    def pool(self) -> Any:
        p = NodePool(_cluster(("a", "10.0.0.1:9527"), ("b", "10.0.0.2:9527")), start_probing=False)
        yield p
        p.stop()

    def test_adds_and_removes(self, pool: NodePool):
        added, removed = pool.replace(_cluster(("a", "10.0.0.1:9527"), ("c", "10.0.0.3:9527")))
        assert added == ["c"]
        assert removed == ["b"]
        assert {s.node.id for s in pool.available()} == {"a", "c"}

    def test_existing_state_is_preserved(self, pool: NodePool):
        """关键：直接重建会把健康计数清零，那样"连续 N 次才切状态"永远达不到，
        熔断与恢复双双失效。
        """
        state = pool.state_for("a")
        assert state is not None
        state.mark_unhealthy("模拟故障")
        state.record_request(success=False)

        pool.replace(_cluster(("a", "10.0.0.1:9527"), ("c", "10.0.0.3:9527")))

        after = pool.state_for("a")
        assert after is state, "同一个 id 必须复用原来那个 NodeState"
        assert after.status is NodeStatus.UNHEALTHY
        assert after.snapshot()["total_failures"] == 1

    def test_address_change_keeps_state_but_updates_config(self, pool: NodePool):
        pool.replace(_cluster(("a", "10.0.0.9:9527"), ("b", "10.0.0.2:9527")))
        state = pool.state_for("a")
        assert state is not None
        assert state.node.address == "10.0.0.9:9527"

    def test_strategy_switches(self, pool: NodePool):
        pool.replace(_cluster(("a", "10.0.0.1:9527"), strategy="random"))
        assert pool.balancer.name == "random"

    def test_state_for_ignores_health(self, pool: NodePool):
        """点名派发要能找到一个被标成 unhealthy 的节点——那恰恰是最该点名试的那个。"""
        state = pool.state_for("b")
        assert state is not None
        state.mark_unhealthy("down")
        assert pool.state_for("b") is state
        assert all(s.node.id != "b" for s in pool.available())

    def test_dns_discovery_is_not_overwritten(self):
        """DNS 模式下节点是解析出来的，``[CLUSTER].nodes`` 只是占位——
        拿它去覆盖等于把发现结果丢掉。
        """
        from ipclick.cluster.discovery import Discovery, DiscoveryConfig

        class FakeDNS(Discovery):
            def resolve(self) -> list[Node]:
                return [Node.from_config({"id": "dns-1", "address": "10.1.1.1:9527"})]

        pool = NodePool(
            _cluster(("a", "10.0.0.1:9527")),
            start_probing=False,
            discovery=FakeDNS(),
            discovery_config=DiscoveryConfig(),
        )
        try:
            added, removed = pool.replace(_cluster(("z", "10.9.9.9:9527"), strategy="random"))
            assert (added, removed) == ([], [])
            assert {s.node.id for s in pool.available()} == {"dns-1"}
            # 策略与阈值仍然要跟着更新
            assert pool.balancer.name == "random"
        finally:
            pool.stop()


# --------------------------------------------------------------------------- #
# 转发器热更新
# --------------------------------------------------------------------------- #


class TestForwarderReload:
    @pytest.fixture
    def service(self) -> Any:
        from ipclick.cluster.forwarder import ForwardingTaskService

        config = Settings(
            {
                "CLUSTER": {
                    "forward": "on",
                    "self_id": "a",
                    "nodes": [
                        {"id": "a", "address": "127.0.0.1:9527"},
                        {"id": "b", "address": "127.0.0.1:9528"},
                    ],
                }
            }
        )
        pool = NodePool(_cluster(("a", "127.0.0.1:9527"), ("b", "127.0.0.1:9528")), start_probing=False)
        svc = ForwardingTaskService(config, pool=pool, server_host="127.0.0.1", server_port=9527)
        yield svc
        svc.cleanup()

    def test_new_node_joins_the_live_pool(self, service: Any):
        """P0-1 的验收点：进程不重启，保存新节点后它立即参与转发轮询。

        断言看的是**正在路由的那份**池子，不是配置文件——0.3 的问题正是这两者
        分了家：文件改了，路由表纹丝不动。
        """
        updated = Settings(
            {
                "CLUSTER": {
                    "forward": "on",
                    "self_id": "a",
                    "nodes": [
                        {"id": "a", "address": "127.0.0.1:9527"},
                        {"id": "b", "address": "127.0.0.1:9528"},
                        {"id": "c", "address": "127.0.0.1:9529"},
                    ],
                }
            }
        )
        ok, message = service.reload_cluster(updated)
        assert ok
        assert "node" not in message or "c" in message
        assert {n.id for n in service.cluster.nodes} == {"a", "b", "c"}
        assert {s.node.id for s in service._pool.available()} == {"a", "b", "c"}

    def test_removed_node_channel_is_closed(self, service: Any):
        closed: list[str] = []

        class FakeChannel:
            def close(self) -> None:
                closed.append("closed")

        service._channels["b"] = FakeChannel()
        service.reload_cluster(
            Settings(
                {"CLUSTER": {"forward": "on", "self_id": "a", "nodes": [{"id": "a", "address": "127.0.0.1:9527"}]}}
            )
        )
        assert closed, "被移除的节点连接要关掉，否则一直挂着占 fd"
        assert "b" not in service._channels

    def test_address_change_drops_the_stale_channel(self, service: Any):
        """id 没变但地址变了：继续用旧 channel 就是往老地址发。"""
        closed: list[str] = []

        class FakeChannel:
            def close(self) -> None:
                closed.append("b")

        service._channels["b"] = FakeChannel()
        service.reload_cluster(
            Settings(
                {
                    "CLUSTER": {
                        "forward": "on",
                        "self_id": "a",
                        "nodes": [
                            {"id": "a", "address": "127.0.0.1:9527"},
                            {"id": "b", "address": "127.0.0.1:19528"},
                        ],
                    }
                }
            )
        )
        assert closed == ["b"]

    def test_identity_is_re_resolved(self, service: Any):
        """本机可能刚被移出列表（从"也干活"变成"只转发"）。"""
        service.reload_cluster(
            Settings({"CLUSTER": {"forward": "on", "nodes": [{"id": "b", "address": "127.0.0.1:9528"}]}})
        )
        assert service.self_id == ""

    def test_bad_config_keeps_the_old_one(self, service: Any):
        """新配置不合法时必须原样保持——半套配置比旧配置危险得多。"""
        before = {n.id for n in service.cluster.nodes}
        ok, message = service.reload_cluster(Settings({"CLUSTER": {"forward": "沙雕", "nodes": []}}))
        assert ok is False
        assert "不合法" in message
        assert {n.id for n in service.cluster.nodes} == before

    def test_send_to_node_rejects_unknown_id(self, service: Any):
        from ipclick.dto.proto import task_pb2
        from ipclick.exceptions import TransportError

        with pytest.raises(TransportError, match="不在集群节点列表里"):
            service.send_to_node(task_pb2.ReqTask(uuid="x", url="http://example.com"), "nope")


# --------------------------------------------------------------------------- #
# 节点探测：三种结论要分得开
# --------------------------------------------------------------------------- #


class _FakeRpcError(grpc.RpcError):
    def __init__(self, code: grpc.StatusCode, details: str = "") -> None:
        self._code = code
        self._details = details

    def code(self) -> grpc.StatusCode:
        return self._code

    def details(self) -> str:
        return self._details


class TestProbeVerdicts:
    """P1-1 的验收点：失败时要能区分"连不上"和"连上了但鉴权不通过"——
    这两种的排查方向完全相反。
    """

    NODE = Node.from_config({"id": "n1", "address": "10.0.0.1:9527"})

    def test_unauthenticated_is_not_unreachable(self):
        result = _from_rpc_error(self.NODE, _FakeRpcError(grpc.StatusCode.UNAUTHENTICATED, "no token"), 0.0)
        assert result.reachable is True
        assert result.authenticated is False
        assert result.ok is False
        assert "IPCLICK_CLUSTER_SECRET" in result.detail

    def test_unavailable_is_unreachable(self):
        result = _from_rpc_error(self.NODE, _FakeRpcError(grpc.StatusCode.UNAVAILABLE, "refused"), 0.0)
        assert result.reachable is False
        assert result.authenticated is None

    def test_old_node_without_ping_counts_as_authenticated(self):
        """UNIMPLEMENTED 说明鉴权拦截器已经放行了——只是对端还是 0.3。
        滚动升级期间不单独说的话，会被误报成鉴权失败。
        """
        result = _from_rpc_error(self.NODE, _FakeRpcError(grpc.StatusCode.UNIMPLEMENTED), 0.0)
        assert result.reachable is True
        assert result.authenticated is True
        assert result.ok is True
        assert "0.4" in result.detail

    def test_unverified_auth_is_not_a_failure(self):
        """内网全互信、不配集群密钥是合法选择。"""
        assert ProbeResult(node_id="n", address="a", reachable=True, authenticated=None, elapsed_ms=1).ok is True


# --------------------------------------------------------------------------- #
# Ping
# --------------------------------------------------------------------------- #


class TestPing:
    def test_reports_identity_and_auth_state(self):
        from ipclick.dto.proto import task_pb2
        from ipclick.services.task_service import TaskService

        service = TaskService(Settings({"CLUSTER": {"self_id": "node-x"}, "SECURITY": {"auth_token": "t"}}))
        try:
            response = service.Ping(task_pb2.PingReq(from_node="node-y"), None)
            assert response.node_id == "node-x"
            assert response.auth_required is True
            assert response.forward is False
            assert response.version
        finally:
            service.cleanup()

    def test_reports_when_the_node_is_wide_open(self):
        """对端没设防是必须让探测方看到的——探测成功本身分不清
        "我的令牌对"和"它根本不验"。
        """
        from ipclick.dto.proto import task_pb2
        from ipclick.services.task_service import TaskService

        service = TaskService(Settings({}))
        try:
            assert service.Ping(task_pb2.PingReq(), None).auth_required is False
        finally:
            service.cleanup()

    def test_ping_leaves_no_trace_record(self):
        """诊断动作不是业务请求，混进请求流只会污染统计。"""
        from ipclick.dto.proto import task_pb2
        from ipclick.services.task_service import TaskService
        from ipclick.trace import get_recorder

        service = TaskService(Settings({}))
        try:
            before = get_recorder().counters.total
            service.Ping(task_pb2.PingReq(), None)
            assert get_recorder().counters.total == before
        finally:
            service.cleanup()


# --------------------------------------------------------------------------- #
# {port} 占位符
# --------------------------------------------------------------------------- #


class TestPortPlaceholder:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("ipclick-trace.{port}.db", "ipclick-trace.9528.db"),
            # 出现在路径中间也要能替换
            ("logs/{port}/app.log", "logs/9528/app.log"),
            ("{port}", "9528"),
            # 没有占位符就原样不动——已有部署的路径绝不能被悄悄改掉
            ("ipclick-trace.db", "ipclick-trace.db"),
            ("stdout", "stdout"),
        ],
    )
    def test_substitution(self, raw: str, expected: str):
        assert placeholders.substitute_port(raw, 9528) == expected

    def test_non_strings_pass_through(self):
        assert placeholders.substitute_port(None, 1) is None
        assert placeholders.substitute_port(42, 1) == 42

    def test_resolve_returns_a_copy(self):
        """load_config 带 lru_cache，返回的是进程内共享的那一个对象。
        就地改它会波及所有调用方——包括配置页，那里必须显示文件里的原始写法。
        """
        section = {"sqlite_path": "t.{port}.db", "retention_days": 30}
        resolved = placeholders.resolve_for("TRACE", section, 9530)
        assert resolved["sqlite_path"] == "t.9530.db"
        assert section["sqlite_path"] == "t.{port}.db", "原 dict 不能被改"
        assert resolved["retention_days"] == 30

    def test_only_declared_keys_are_touched(self):
        """无差别替换所有像路径的值只会制造惊吓——证书路径按端口分离没有意义。"""
        section = {"cert_file": "/etc/{port}/a.pem"}
        assert placeholders.resolve_for("SECURITY", section, 9527) == section

    def test_no_literal_placeholder_survives(self):
        """未指定端口时用实际生效的默认端口，不要留下字面量 {port}。"""
        for section_name, keys in placeholders.PORT_AWARE_KEYS.items():
            section = dict.fromkeys(keys, "x.{port}.y")
            resolved = placeholders.resolve_for(section_name, section, 9527)
            assert all("{port}" not in v for v in resolved.values())

    def test_default_template_ships_with_the_placeholder(self):
        """新用户默认就该拿到按端口分离的行为。"""
        from ipclick.config_loader.loader import example_config

        text = example_config()
        assert "ipclick-trace.{port}.db" in text
