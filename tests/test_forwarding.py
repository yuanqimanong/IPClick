"""服务端转发集群的端到端测试。

起真的 gRPC 服务端（三个进程内实例），走真的 TCP 与真的转发，只把最外层的
HTTP 下载换成假适配器——要验的是路由、鉴权与防环，不是 HTTP。
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent import futures
import socket
import threading
from typing import Any

import grpc
import pytest

from ipclick.auth import TokenAuthInterceptor
from ipclick.cluster.forwarder import ForwardingTaskService, resolve_self_id
from ipclick.cluster.node import ClusterConfig
from ipclick.cluster.tokens import cluster_secret, derive_token, self_tokens
from ipclick.dto.proto import task_pb2, task_pb2_grpc
from ipclick.dto.response import Response
from ipclick.services.task_service import TaskService
from ipclick.utils.config_util import Settings


SECRET = "test-cluster-secret"

#: 开发机普遍设了 http_proxy，而 gRPC 默认会读它——不关掉的话连本机都连不上
_NO_PROXY = [("grpc.enable_http_proxy", 0)]


class FakeAdapter:
    """假适配器：不发真请求，把自己所在的节点名写进响应体。"""

    adapter_name: str = "curl_cffi"

    def __init__(self, node_id: str) -> None:
        self.node_id: str = node_id
        self.calls: int = 0

    def download(self, url: str, **kwargs: Any) -> Response:
        self.calls += 1
        return Response(url=url, status_code=200, content=self.node_id.encode(), headers={})

    def download_stream(self, url: str, **kwargs: Any) -> Iterator[Any]:
        raise NotImplementedError

    def close(self) -> None: ...


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Node:
    """一个进程内的 IPClick 节点。"""

    def __init__(self, node_id: str, port: int, nodes: list[dict[str, Any]], *, forward: bool, secret: str = SECRET):
        self.id: str = node_id
        self.port: int = port
        cluster_section: dict[str, Any] = {
            "nodes": nodes,
            "forward": "on" if forward else "off",
            "self_id": node_id,
            "secret": secret,
            "probe_interval": 3600,  # 测试里不需要后台探活反复打扰
            "max_failover": 3,
        }
        config = Settings({"CLUSTER": cluster_section, "SERVER": {"max_workers": 4}})
        cluster = ClusterConfig.from_config(cluster_section)

        self.service: TaskService
        if forward:
            self.service = ForwardingTaskService(config, cluster, server_port=port)
        else:
            self.service = TaskService(config)

        self.adapter: FakeAdapter = FakeAdapter(node_id)
        self.service._adapter_cache["curl_cffi"] = self.adapter  # pyright: ignore[reportPrivateUsage]
        self.service.default_adapter = self.adapter  # pyright: ignore[reportAttributeAccessIssue]

        tokens = self_tokens(node_id, "", cluster_secret(cluster_section))
        self.server: grpc.Server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=4),
            interceptors=[TokenAuthInterceptor(tokens)],
        )
        task_pb2_grpc.add_TaskServiceServicer_to_server(self.service, self.server)
        _ = self.server.add_insecure_port(f"127.0.0.1:{port}")
        self.server.start()

    def stop(self) -> None:
        _ = self.server.stop(grace=0)
        self.service.cleanup()


def _call(port: int, token: str | None, url: str = "http://example.com/x") -> task_pb2.TaskResp:
    with grpc.insecure_channel(f"127.0.0.1:{port}", options=_NO_PROXY) as channel:
        stub = task_pb2_grpc.TaskServiceStub(channel)
        metadata = (("authorization", f"Bearer {token}"),) if token else None
        return stub.Send(task_pb2.ReqTask(uuid="u1", url=url), timeout=10, metadata=metadata)


@pytest.fixture
def cluster() -> Iterator[list[Node]]:
    """三节点：a 是入口（开转发），b / c 是子节点（不转发）。"""
    ports = {name: _free_port() for name in ("a", "b", "c")}
    entries = [{"id": name, "address": f"127.0.0.1:{port}"} for name, port in ports.items()]
    nodes = [
        Node("a", ports["a"], entries, forward=True),
        Node("b", ports["b"], entries, forward=False),
        Node("c", ports["c"], entries, forward=False),
    ]
    try:
        yield nodes
    finally:
        for node in nodes:
            node.stop()


class TestForwarding:
    def test_round_robin_reaches_every_node(self, cluster: list[Node]):
        """连发 6 次，三个节点都该干过活——入口自己也在轮询里。"""
        entry = cluster[0]
        token = derive_token(SECRET, "a")
        executors = [_call(entry.port, token).content.decode() for _ in range(6)]
        assert set(executors) == {"a", "b", "c"}, f"实际落点: {executors}"
        assert sum(n.adapter.calls for n in cluster) == 6

    def test_trace_reports_real_executor(self, cluster: list[Node]):
        token = derive_token(SECRET, "a")
        seen: set[str] = set()
        for _ in range(6):
            response = _call(cluster[0].port, token)
            # 谁真正执行的，trace 里就该是谁——这正是 original_request 换成 trace 的目的
            assert response.HasField("trace")
            assert response.trace.node_id == response.content.decode()
            seen.add(response.trace.node_id)
            if response.trace.node_id != "a":
                assert response.trace.forwarded is True, "子节点应能从 metadata 看出自己是被转发的"
        assert seen == {"a", "b", "c"}

    def test_forwarded_request_is_never_forwarded_again(self, cluster: list[Node]):
        """防环：带着转发标记直连子节点，它必须自己执行。"""
        sub = cluster[1]
        with grpc.insecure_channel(f"127.0.0.1:{sub.port}", options=_NO_PROXY) as channel:
            stub = task_pb2_grpc.TaskServiceStub(channel)
            response = stub.Send(
                task_pb2.ReqTask(uuid="u1", url="http://example.com/x"),
                timeout=10,
                metadata=(
                    ("ipclick-forwarded", "1"),
                    ("authorization", f"Bearer {derive_token(SECRET, 'b')}"),
                ),
            )
        assert response.content == b"b"
        assert response.trace.forwarded is True

    def test_any_node_can_be_the_entry(self):
        """对等入口：三台机器用同一份配置（forward=on），随便打哪台都能分发。"""
        ports = {name: _free_port() for name in ("a", "b", "c")}
        entries = [{"id": name, "address": f"127.0.0.1:{port}"} for name, port in ports.items()]
        nodes = [Node(name, port, entries, forward=True) for name, port in ports.items()]
        try:
            for entry in nodes:
                token = derive_token(SECRET, entry.id)
                executors = {_call(entry.port, token).content.decode() for _ in range(6)}
                assert executors == {"a", "b", "c"}, f"入口 {entry.id} 的落点: {executors}"
        finally:
            for node in nodes:
                node.stop()

    def test_entry_takes_over_when_subnodes_die(self, cluster: list[Node]):
        """子节点全挂时入口自己兜底，而不是让请求失败。"""
        for node in cluster[1:]:
            node.stop()
        token = derive_token(SECRET, "a")
        for _ in range(4):
            response = _call(cluster[0].port, token)
            assert response.status_code == 200
            assert response.content == b"a"

    def test_wrong_token_is_rejected_by_subnode(self, cluster: list[Node]):
        """每节点令牌独立：拿 b 的令牌调 c 应该被拒。"""
        with grpc.insecure_channel(f"127.0.0.1:{cluster[2].port}", options=_NO_PROXY) as channel:
            stub = task_pb2_grpc.TaskServiceStub(channel)
            with pytest.raises(grpc.RpcError) as excinfo:
                _ = stub.Send(
                    task_pb2.ReqTask(uuid="u1", url="http://example.com/x"),
                    timeout=10,
                    metadata=(("authorization", f"Bearer {derive_token(SECRET, 'b')}"),),
                )
        assert excinfo.value.code() is grpc.StatusCode.UNAUTHENTICATED  # pyright: ignore[reportAttributeAccessIssue]

    def test_batch_is_spread_across_nodes(self, cluster: list[Node]):
        """批量是转发模式最划算的场景：一次调用摊到多台机器上。"""
        token = derive_token(SECRET, "a")
        with grpc.insecure_channel(f"127.0.0.1:{cluster[0].port}", options=_NO_PROXY) as channel:
            stub = task_pb2_grpc.TaskServiceStub(channel)
            requests = (task_pb2.ReqTask(uuid=f"u{i}", url=f"http://example.com/{i}") for i in range(9))
            executors = [
                r.content.decode()
                for r in stub.SendBatch(requests, timeout=20, metadata=(("authorization", f"Bearer {token}"),))
            ]
        assert len(executors) == 9
        assert set(executors) == {"a", "b", "c"}

    def test_stream_is_not_forwarded(self, cluster: list[Node]):
        """流式永远本地执行——假适配器不支持流式，所以入口自己会报错，
        而不是把请求转给别人。"""
        entry = cluster[0]
        before = [n.adapter.calls for n in cluster[1:]]
        with grpc.insecure_channel(f"127.0.0.1:{entry.port}", options=_NO_PROXY) as channel:
            stub = task_pb2_grpc.TaskServiceStub(channel)
            chunks = list(
                stub.SendStream(
                    task_pb2.ReqTask(uuid="u1", url="http://example.com/big"),
                    timeout=10,
                    metadata=(("authorization", f"Bearer {derive_token(SECRET, 'a')}"),),
                )
            )
        assert chunks, "至少该收到 header 与 trailer"
        assert [n.adapter.calls for n in cluster[1:]] == before, "流式不该落到子节点上"


class TestSelfIdentity:
    def test_explicit_self_id_wins(self):
        cluster = ClusterConfig.from_config(
            {"nodes": [{"id": "x", "address": "10.0.0.1:9527"}], "self_id": "x", "forward": "on"}
        )
        assert resolve_self_id(cluster, "0.0.0.0", 9527) == "x"

    def test_auto_detect_by_port_and_local_address(self):
        cluster = ClusterConfig.from_config(
            {
                "nodes": [
                    {"id": "local", "address": "127.0.0.1:19527"},
                    {"id": "other", "address": "10.9.9.9:19527"},
                ],
                "forward": "on",
            }
        )
        assert resolve_self_id(cluster, "127.0.0.1", 19527) == "local"

    def test_standalone_is_silent(self):
        """回归：单机部署（没有 nodes）曾经也会打一条"本节点将只转发"的告警，
        而那句话在转发关着时根本是错的。
        """
        messages: list[str] = []
        from ipclick.utils.log_util import logger

        sink = logger.add(lambda m: messages.append(m.record["message"]), level="WARNING")
        try:
            assert resolve_self_id(ClusterConfig.from_config({}), "127.0.0.1", 9527) == ""
        finally:
            logger.remove(sink)
        assert messages == [], messages

    def test_forward_off_does_not_warn_about_forwarding(self):
        """节点列表有内容但转发关着：不该说"只转发不执行"。"""
        messages: list[str] = []
        from ipclick.utils.log_util import logger

        sink = logger.add(lambda m: messages.append(m.record["message"]), level="WARNING")
        try:
            cluster = ClusterConfig.from_config({"nodes": [{"id": "other", "address": "10.9.9.9:9527"}]})
            assert resolve_self_id(cluster, "127.0.0.1", 12345) == ""
        finally:
            logger.remove(sink)
        assert not any("只转发" in m for m in messages), messages

    def test_unknown_self_is_empty_not_an_error(self):
        """识别不出来只转发不执行，且不成环——不该让服务起不来。"""
        cluster = ClusterConfig.from_config({"nodes": [{"id": "other", "address": "10.9.9.9:9527"}], "forward": "on"})
        assert resolve_self_id(cluster, "127.0.0.1", 12345) == ""


class TestForwardConfig:
    def test_forward_defaults_off(self):
        assert ClusterConfig.from_config({"nodes": [{"address": "1.2.3.4:9527"}]}).forwarding_enabled is False

    def test_boolean_spelling_accepted(self):
        cluster = ClusterConfig.from_config({"nodes": [{"address": "1.2.3.4:9527"}], "forward": True})
        assert cluster.forwarding_enabled is True

    def test_unknown_value_raises(self):
        from ipclick.exceptions import ConfigError

        with pytest.raises(ConfigError, match="forward"):
            _ = ClusterConfig.from_config({"forward": "maybe"})

    def test_forward_without_nodes_is_inert(self):
        """开了转发但没有节点：没得转，退化成单机，不该报错。"""
        assert ClusterConfig.from_config({"forward": "on"}).forwarding_enabled is False


class TestTokenDerivation:
    def test_each_node_gets_a_different_token(self):
        tokens = {derive_token(SECRET, f"node-{i}") for i in range(20)}
        assert len(tokens) == 20

    def test_derivation_is_stable(self):
        assert derive_token(SECRET, "a") == derive_token(SECRET, "a")

    def test_explicit_token_overrides_derived(self):
        from ipclick.cluster.tokens import token_for

        assert token_for("a", "written-in-config", SECRET) == "written-in-config"

    def test_no_secret_means_no_token(self):
        from ipclick.cluster.tokens import token_for

        assert token_for("a", "", "") is None


def test_forwarding_is_thread_safe_under_concurrency(cluster: list[Node]):
    """并发下路由不能串台：每条响应的 trace 必须和它的执行者一致。"""
    token = derive_token(SECRET, "a")
    results: list[tuple[str, str]] = []
    lock = threading.Lock()

    def worker() -> None:
        response = _call(cluster[0].port, token)
        with lock:
            results.append((response.trace.node_id, response.content.decode()))

    threads = [threading.Thread(target=worker) for _ in range(15)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert len(results) == 15
    assert all(trace_node == body for trace_node, body in results), results


class TestForwardTimeoutCoversBrowserBudget:
    """转发的截止时间必须不小于子节点自己的预算。

    小了的后果实测过：入口 135 秒放弃、把节点摘掉、换一台重发，而被放弃的那台
    并不停工，又白跑了 161 秒——三份重复渲染同时压在一台机器上。
    """

    def _service(self, **browser: object) -> ForwardingTaskService:
        section: dict[str, Any] = {
            "nodes": [{"id": "peer", "address": "127.0.0.1:19999"}],
            "forward": "on",
            "self_id": "me",
            "probe_interval": 3600,
        }
        config = Settings({"CLUSTER": section, "BROWSER": browser})
        return ForwardingTaskService(config, ClusterConfig.from_config(section))

    def test_browser_request_gets_a_much_longer_deadline(self):
        service = self._service(engine="playwright")
        try:
            http_req = task_pb2.ReqTask(url="http://x/", timeout_seconds=30, max_retries=0)
            browser_req = task_pb2.ReqTask(url="http://x/", timeout_seconds=30, max_retries=0, adapter=task_pb2.BROWSER)
            http_deadline = service._timeout_for(http_req)  # pyright: ignore[reportPrivateUsage]
            browser_deadline = service._timeout_for(browser_req)  # pyright: ignore[reportPrivateUsage]
            assert browser_deadline > http_deadline
            # 至少要覆盖 页面加载 + 脚本 + 冷启动
            assert browser_deadline >= 30 + 60 + 60
        finally:
            service.cleanup()

    def test_explicit_forward_timeout_still_wins(self):
        section: dict[str, Any] = {
            "nodes": [{"id": "peer", "address": "127.0.0.1:19999"}],
            "forward": "on",
            "self_id": "me",
            "probe_interval": 3600,
            "forward_timeout": 42,
        }
        service = ForwardingTaskService(Settings({"CLUSTER": section}), ClusterConfig.from_config(section))
        try:
            req = task_pb2.ReqTask(url="http://x/", adapter=task_pb2.BROWSER)
            assert service._timeout_for(req) == 42  # pyright: ignore[reportPrivateUsage]
        finally:
            service.cleanup()


class TestSlowRequestDoesNotEvictHealthyNodes:
    """一个慢请求只说明这一个请求慢，不说明那台机器坏了。

    把健康节点摘掉之后流量全压到剩下的机器上、让它们也开始超时——
    这是能把整个集群推倒的正反馈。节点健康与否交给后台探活判定。
    """

    @pytest.mark.parametrize(
        ("code", "should_evict"),
        [
            (grpc.StatusCode.UNAVAILABLE, True),
            (grpc.StatusCode.DEADLINE_EXCEEDED, False),
            (grpc.StatusCode.RESOURCE_EXHAUSTED, False),
            (grpc.StatusCode.INTERNAL, False),
            (grpc.StatusCode.UNKNOWN, False),
        ],
    )
    def test_only_unreachable_marks_unhealthy(self, code: grpc.StatusCode, should_evict: bool):
        from ipclick.cluster.forwarder import _is_node_fault, _should_mark_unhealthy

        error = grpc.RpcError()
        error.code = lambda: code  # pyright: ignore[reportAttributeAccessIssue]
        # 这几个码都值得换一台再试
        assert _is_node_fault(error) is True
        assert _should_mark_unhealthy(error) is should_evict
