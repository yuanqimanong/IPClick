from __future__ import annotations

from collections import Counter
from typing import Any

import pytest

from ipclick.cluster.balancer import RoundRobinBalancer, create_balancer
from ipclick.cluster.node import ClusterConfig, Node, NodeState, NodeStatus
from ipclick.cluster.tokens import CLUSTER_SECRET_ENV, cluster_secret, derive_token, self_tokens, token_for
from ipclick.exceptions import ConfigError


def _states(count: int, weight: int = 100) -> list[NodeState]:
    return [NodeState(Node(id=f"n{i}", host="127.0.0.1", port=9600 + i, weight=weight)) for i in range(count)]


def test_round_robin_splits_traffic_exactly() -> None:
    balancer = RoundRobinBalancer()
    states = _states(4)
    picks = Counter(balancer.pick(states).node.id for _ in range(400))
    assert picks == Counter({"n0": 100, "n1": 100, "n2": 100, "n3": 100})


def test_weighted_balancer_follows_the_weights() -> None:
    balancer = create_balancer("weight")
    heavy = NodeState(Node(id="heavy", host="h", port=1, weight=1000))
    light = NodeState(Node(id="light", host="h", port=2, weight=1))
    picks = Counter(balancer.pick([heavy, light]).node.id for _ in range(500))
    assert picks["heavy"] > picks["light"] * 10


def test_balancer_aliases_and_unknown_strategy() -> None:
    assert create_balancer("rr").name == "round_robin"
    assert create_balancer("weighted").name == "weight"
    assert create_balancer(" RANDOM ").name == "random"
    with pytest.raises(ConfigError, match="负载均衡策略"):
        create_balancer("magic")


def test_node_from_config_parses_address() -> None:
    node = Node.from_config({"address": "10.0.0.1:9601", "weight": 50, "tags": ["a", 1]})
    assert (node.id, node.host, node.port, node.weight) == ("10.0.0.1:9601", "10.0.0.1", 9601, 50)
    assert node.tags == ("a", "1")
    assert Node.from_config({"address": "[::1]:9601"}).host == "::1"


@pytest.mark.parametrize("entry", [{}, {"address": "no-port"}, {"address": "h:port"}])
def test_node_from_config_rejects_bad_addresses(entry: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        Node.from_config(entry)


def test_cluster_config_defaults() -> None:
    config = ClusterConfig.from_config({})
    assert config.nodes == ()
    assert config.strategy == "round_robin"
    assert config.forward == "off"
    assert config.forwarding_enabled is False


def test_forwarding_needs_both_the_switch_and_nodes() -> None:
    nodes = [{"address": "127.0.0.1:9601"}]
    assert ClusterConfig.from_config({"forward": "on", "nodes": nodes}).forwarding_enabled is True
    assert ClusterConfig.from_config({"forward": "on"}).forwarding_enabled is False
    assert ClusterConfig.from_config({"forward": "yes", "nodes": nodes}).forward == "on"
    assert ClusterConfig.from_config({"forward": "0", "nodes": nodes}).forward == "off"
    with pytest.raises(ConfigError, match="forward"):
        ClusterConfig.from_config({"forward": "maybe"})


def test_max_failover_zero_means_no_failover() -> None:
    assert ClusterConfig.from_config({"max_failover": 0}).max_failover == 0
    assert ClusterConfig.from_config({"max_failover": -1}).max_failover == 2
    assert ClusterConfig.from_config({"max_failover": "two"}).max_failover == 2


def test_unknown_keys_are_kept_in_extra() -> None:
    config = ClusterConfig.from_config({"discovery": {"mode": "dns"}})
    assert config.extra == {"discovery": {"mode": "dns"}}


def test_node_state_flips_only_after_the_threshold() -> None:
    state = _states(1)[0]
    assert state.status is NodeStatus.UNKNOWN
    assert state.is_available is True

    assert state.record_probe(False, "boom", failure_threshold=2, recovery_threshold=2) is False
    assert state.status is NodeStatus.UNKNOWN
    assert state.record_probe(False, "boom", failure_threshold=2, recovery_threshold=2) is True
    assert state.status is NodeStatus.UNHEALTHY
    assert state.is_available is False

    assert state.record_probe(True, "", failure_threshold=2, recovery_threshold=2) is False
    assert state.record_probe(True, "", failure_threshold=2, recovery_threshold=2) is True
    assert state.status is NodeStatus.HEALTHY


def test_derived_tokens_are_stable_and_bound_to_the_node_id() -> None:
    first = derive_token("s3cret", "node-a")
    assert first == derive_token("s3cret", "node-a")
    assert first != derive_token("s3cret", "node-b")
    assert first != derive_token("other", "node-a")
    assert "=" not in first


def test_explicit_token_wins_over_the_derived_one() -> None:
    assert token_for("node-a", explicit=" tok ", secret="s") == "tok"
    assert token_for("node-a", explicit="", secret="s") == derive_token("s", "node-a")
    assert token_for("node-a") is None


def test_self_tokens_accepts_both_and_dedupes() -> None:
    assert self_tokens("node-a", "tok", "s") == ("tok", derive_token("s", "node-a"))
    assert self_tokens("", "", "s") == ()
    assert self_tokens("node-a", "tok", "") == ("tok",)


def test_cluster_secret_prefers_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert cluster_secret({"secret": "from-config"}) == "from-config"
    monkeypatch.setenv(CLUSTER_SECRET_ENV, " from-env ")
    assert cluster_secret({"secret": "from-config"}) == "from-env"


class _FakeChannel:
    """只记录 close 是否被调用的假 channel。"""

    def __init__(self) -> None:
        self.closed: bool = False

    def close(self) -> None:
        self.closed = True


def _forwarder_with_fake_channels(monkeypatch: pytest.MonkeyPatch) -> Any:
    """构造一个只装了 channel 缓存的转发器，避免起真实服务。"""
    import threading

    from ipclick.cluster import forwarder as fwd

    service: Any = object.__new__(fwd.ForwardingTaskService)
    service._channels = {}
    service._channels_lock = threading.Lock()
    service._tls = None
    monkeypatch.setattr(fwd, "open_channel_for", lambda target, tls: _FakeChannel())
    from ipclick.dto.proto import task_pb2_grpc

    monkeypatch.setattr(task_pb2_grpc, "TaskServiceStub", lambda channel: object())
    return service


def test_channel_is_not_closed_while_a_request_still_holds_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """热更新摘除节点时，被在途请求持有的 channel 不得当场关闭。

    在已 close() 的 channel 上构造 stub 会让进程直接段错误，在它上面发起调用则抛
    ValueError（不是 RpcError，两处 except 都接不住）。所以 close 必须推迟到最后一个
    使用者放手之后。
    """
    service = _forwarder_with_fake_channels(monkeypatch)

    with service._leased_stub("n1", "127.0.0.1", 9528):
        entry = service._channels["n1"]
        channel = entry.channel
        assert entry.users == 1

        service._close_channels({"n1"})

        # 已从缓存摘除（后续请求拿不到它），但因为还有人用着，不能关
        assert "n1" not in service._channels
        assert channel.closed is False
        assert entry.retired is True

    # 最后一个使用者退出后才真正关闭
    assert channel.closed is True


def test_channel_closes_immediately_when_nobody_holds_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有在途使用者时，摘除即关闭，不留悬挂连接。"""
    service = _forwarder_with_fake_channels(monkeypatch)

    with service._leased_stub("n1", "127.0.0.1", 9528):
        pass
    channel = service._channels["n1"].channel

    service._close_channels({"n1"})

    assert channel.closed is True
    assert "n1" not in service._channels


def test_leased_stub_reuses_one_channel_per_node(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一节点的多次租借复用同一个 channel，且计数正确回落到零。"""
    service = _forwarder_with_fake_channels(monkeypatch)

    with service._leased_stub("n1", "127.0.0.1", 9528):
        with service._leased_stub("n1", "127.0.0.1", 9528):
            assert service._channels["n1"].users == 2
        assert service._channels["n1"].users == 1
    assert service._channels["n1"].users == 0
    assert service._channels["n1"].channel.closed is False


@pytest.mark.parametrize(
    ("method", "replayable"),
    [
        ("GET", True),
        ("HEAD", True),
        ("OPTIONS", True),
        ("POST", False),
        ("PUT", False),
        ("PATCH", False),
        ("DELETE", False),
    ],
)
def test_client_failover_only_replays_read_methods(method: str, replayable: bool) -> None:
    """客户端分发的故障转移必须和服务端转发口径一致：只有读方法能换节点重投。

    传输层错误意味着结果未知——下游可能已经把订单建好了、只是回复没赶上，
    重投一次就是重复下单。
    """
    from ipclick.cluster.client import _is_replayable
    from ipclick.dto.models import DownloadTask, HttpMethod

    task = DownloadTask(url="https://api.example.com/orders", method=HttpMethod[method])
    assert _is_replayable(task) is replayable


def test_client_failover_matches_the_server_side_whitelist() -> None:
    """两侧的白名单不该各自漂移。"""
    from ipclick.cluster.client import _REPLAYABLE_METHODS
    from ipclick.cluster.forwarder import _FAILOVER_SAFE_METHODS

    assert {m.name for m in _REPLAYABLE_METHODS} == set(_FAILOVER_SAFE_METHODS)
