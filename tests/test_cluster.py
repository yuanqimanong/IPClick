from __future__ import annotations

from collections import Counter

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
