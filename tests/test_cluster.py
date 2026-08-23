from __future__ import annotations

from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, final

import pytest
from typing_extensions import override

from ipclick.cluster.balancer import RoundRobinBalancer, create_balancer
from ipclick.cluster.node import ClusterConfig, Node, NodeState, NodeStatus
from ipclick.cluster.tokens import CLUSTER_SECRET_ENV, cluster_secret, derive_token, self_tokens, token_for
from ipclick.dto.proto import task_pb2
from ipclick.exceptions import ConfigError
from ipclick.services.detached import DetachedContext


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


def test_leased_stub_does_not_leak_a_user_when_the_stub_cannot_be_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stub 构造失败不能留下一个永远减不回去的使用者。

    ``entry.users += 1`` 和 ``TaskServiceStub(...)`` 都在 ``with self._channels_lock``
    里、在 ``try:`` **之外**。构造抛异常时异常从锁块直接逃出，``finally`` 根本不执行，
    ``users`` 永久 +1——这个 channel 被 retire 之后再也不会 close，连接和 fd 就此泄漏。
    """
    from ipclick.dto.proto import task_pb2_grpc

    service = _forwarder_with_fake_channels(monkeypatch)

    def _explode(_channel: object) -> object:
        raise RuntimeError("stub 构造失败")

    monkeypatch.setattr(task_pb2_grpc, "TaskServiceStub", _explode)

    with pytest.raises(RuntimeError), service._leased_stub("n1", "127.0.0.1", 9528):
        pass  # pragma: no cover - 进不来

    entry = service._channels["n1"]
    assert entry.users == 0

    # 计数正确才能在摘除时真的关掉；漏减的话这里会停在 retired 而永不 close
    service._close_channels({"n1"})
    assert entry.channel.closed is True


class _LockedOnlyDict(dict[str, Any]):
    """只允许在持有指定锁时迭代的 dict，用来钉住"读 channel 表必须上锁"。"""

    def __init__(self, lock: Any) -> None:
        super().__init__()
        self._lock: Any = lock

    @override
    def __iter__(self) -> Any:
        if not self._lock.locked():
            raise AssertionError("读 _channels 必须在 _channels_lock 内")
        return super().__iter__()


class _StoppablePool:
    def __init__(self) -> None:
        self.stopped: bool = False

    def stop(self) -> None:
        self.stopped = True


def test_shutdown_snapshots_the_channel_table_under_the_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """停机时读 channel 表必须上锁。

    ``set(self._channels)`` 不加锁地迭代，而 ``_leased_stub`` 会在别的线程往同一个
    dict 里插新节点——正好在停机这条路上撞出 "dictionary changed size during
    iteration"，把优雅停机变成一个异常。
    """
    from ipclick.cluster import forwarder as fwd
    from ipclick.services.task_service import TaskService

    service = _forwarder_with_fake_channels(monkeypatch)
    service._channels = _LockedOnlyDict(service._channels_lock)
    service._pool = _StoppablePool()
    monkeypatch.setattr(TaskService, "cleanup", lambda self: None)

    with service._leased_stub("n1", "127.0.0.1", 9528):
        pass

    fwd.ForwardingTaskService.cleanup(service)

    assert service._pool.stopped is True
    assert "n1" not in service._channels


async def test_async_shutdown_snapshots_the_channel_table_under_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """acleanup 与 cleanup 同理，同一处不加锁的迭代。"""
    from ipclick.cluster import forwarder as fwd
    from ipclick.services.task_service import TaskService

    service = _forwarder_with_fake_channels(monkeypatch)
    service._channels = _LockedOnlyDict(service._channels_lock)
    service._pool = _StoppablePool()

    async def _noop(_self: object) -> None:
        return None

    monkeypatch.setattr(TaskService, "acleanup", _noop)

    with service._leased_stub("n1", "127.0.0.1", 9528):
        pass

    await fwd.ForwardingTaskService.acleanup(service)

    assert service._pool.stopped is True
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


def test_node_pool_counts_unknown_nodes_as_live() -> None:
    """限流分片的节点计数必须把 UNKNOWN 也算进去。

    节点初始状态是 UNKNOWN。只认 HEALTHY 的话，从启动到第一轮探活完成的这段窗口里
    计数是 0，分片直接不生效——每台都按完整 per_host_qps 跑，N 台集群就是 N 倍的
    全局限额打到目标站点上。限流这件事上"没证据说它挂了就假定它在"是更安全的方向。
    """
    from ipclick.cluster.node import ClusterConfig
    from ipclick.cluster.pool import NodePool

    config = ClusterConfig.from_config(
        {
            "forward": "off",
            "nodes": [
                {"id": "n1", "address": "10.0.0.1:9528"},
                {"id": "n2", "address": "10.0.0.2:9528"},
                {"id": "n3", "address": "10.0.0.3:9528"},
            ],
        }
    )
    pool = NodePool(config)
    try:
        seen: list[int] = []
        pool.on_health_change(seen.append)
        pool._notify_health()

        # 三个节点都还没探活过（UNKNOWN），仍应报 3 而不是 0
        assert seen == [3]

        # 明确判定为不健康的才不计入
        for state in pool._states:
            if state.node.id == "n2":
                state.mark_unhealthy("probe failed")
        seen.clear()
        pool._notify_health()
        assert seen == [2]
    finally:
        pool.stop()


def test_transport_error_carries_the_grpc_code() -> None:
    """集群客户端要靠原始状态码区分"连不上"和"服务端内部出错"。

    之前对任何 TransportError 都立即摘除节点，于是抓一个让服务端报错的目标
    （INTERNAL / UNKNOWN）就能把整个集群摘空，而且绕过了 failure_threshold 的迟滞。
    """
    from typing import Any as _Any

    import grpc as _grpc

    from ipclick.sdk import Downloader

    class _Err(_grpc.RpcError):
        def __init__(self, code: _Any) -> None:
            self._code: _Any = code

        @override
        def code(self) -> _Any:
            return self._code

        @override
        def details(self) -> str:
            return "boom"

    downloader: _Any = object.__new__(Downloader)
    downloader.port = 9528
    downloader._metadata = ()

    for code in (_grpc.StatusCode.UNAVAILABLE, _grpc.StatusCode.INTERNAL, _grpc.StatusCode.UNKNOWN):
        error = downloader._rpc_error(_Err(code))
        assert getattr(error, "grpc_code", None) is code


class _RecordingLimiter:
    """记录 acquire 过哪些 URL 的假限流器。"""

    def __init__(self) -> None:
        self.acquired: list[str] = []

    @contextmanager
    def acquire(self, url: str) -> Generator[None]:
        self.acquired.append(url)
        yield


def _forwarder_that_forwards(monkeypatch: pytest.MonkeyPatch, limiter: _RecordingLimiter) -> Any:
    """装一个"必定转发到远端节点"的转发器，只留下限流这条链路可观察。"""
    from ipclick.cluster import forwarder as fwd
    from ipclick.cluster.node import ClusterConfig, Node

    service: Any = object.__new__(fwd.ForwardingTaskService)
    service.self_id = "entry"
    service.cluster = ClusterConfig(forward="on", nodes=(Node(id="n1", host="10.0.0.1", port=9528),), max_failover=0)
    service.host_limiter = limiter
    service._local_count = 0
    service._forwarded_count = 0

    remote = _StubState(Node(id="n1", host="10.0.0.1", port=9528))
    service._pool = _StubPool(remote)

    forwarded: list[Any] = []

    def _forward(_state: Any, request: Any) -> Any:
        forwarded.append(request)
        return task_pb2.TaskResp(request_uuid=request.uuid, status_code=200)

    monkeypatch.setattr(service, "_forward", _forward)
    monkeypatch.setattr(service, "_reject_if_url_not_allowed", lambda _r, _c: None)
    service.forwarded = forwarded
    return service


@final
class _StubState:
    def __init__(self, node: Any) -> None:
        self.node: Any = node
        self.successes: int = 0

    def record_request(self, *, success: bool) -> None:
        self.successes += int(success)

    def mark_unhealthy(self, _reason: str) -> None:
        return None


@final
class _StubPool:
    def __init__(self, state: _StubState) -> None:
        self._state: _StubState = state

    def acquire(self, *, exclude: set[str]) -> _StubState:
        _ = exclude
        return self._state


def test_the_entry_node_rate_limits_forwarded_traffic(monkeypatch: pytest.MonkeyPatch) -> None:
    """forward = "on" 时转发流量必须过入口节点的限流闸门。

    原来只有本地执行那一支过闸（_execute_download 里的 acquire），转发这一支完全不过；
    而 server._wire_rate_sharding 在开了转发时直接返回、不做分片，于是每个下游节点各按
    完整配额放行——配 10 QPS 部三台，目标站点实际挨 30。default_config 里写的恰好是
    反过来的承诺（"forward = on 精确……要精确限流，这是最省事的形态"），而且这种偏差
    从任何单台机器的视角看都完全正常，极难自查。
    """
    limiter = _RecordingLimiter()
    service = _forwarder_that_forwards(monkeypatch, limiter)
    request = task_pb2.ReqTask(uuid="u1", url="https://target.example/a")

    response = service.Send(request, DetachedContext().as_servicer_context())

    assert response.request_uuid == "u1"
    assert response.status_code == 200
    assert len(service.forwarded) == 1
    assert limiter.acquired == ["https://target.example/a"]


def _pool_with(*node_ids: str) -> Any:
    from ipclick.cluster.pool import NodePool

    config = ClusterConfig(nodes=tuple(Node(id=i, host="10.0.0.1", port=9528) for i in node_ids))
    return NodePool(config, start_probing=False)


def test_a_removed_and_readded_node_does_not_stay_drained() -> None:
    """摘除标记必须跟着节点列表一起裁剪。

    _drained 原来只在 drain/undrain 里增删，replace() 重建 _states 时不管它。于是
    "摘除 n1 → 从配置里删掉 n1 → 又把 n1 加回来"之后，n1 的 id 仍留在 _drained 里，
    available() 一直把它滤掉——新加回来的节点永远分不到流量，而快照里只显示它
    "已摘除"，看不出为什么。DNS 发现下 id 还会不断变化，这个集合只增不减。
    """
    pool = _pool_with("n1", "n2")
    assert pool.drain("n1") is True
    assert [s.node.id for s in pool.available()] == ["n2"]

    # 把 n1 从配置里摘掉
    _ = pool.replace(ClusterConfig(nodes=(Node(id="n2", host="10.0.0.1", port=9528),)))
    # 再加回来
    _ = pool.replace(
        ClusterConfig(nodes=(Node(id="n2", host="10.0.0.1", port=9528), Node(id="n1", host="10.0.0.1", port=9528)))
    )

    assert sorted(s.node.id for s in pool.available()) == ["n1", "n2"]
    assert [n["id"] for n in pool.snapshot()["nodes"] if n["drained"]] == []


def test_draining_still_works_across_an_unrelated_replace() -> None:
    """裁剪不能把仍在配置里的摘除标记也抹掉。"""
    pool = _pool_with("n1", "n2")
    assert pool.drain("n1") is True

    _ = pool.replace(
        ClusterConfig(nodes=(Node(id="n1", host="10.0.0.1", port=9528), Node(id="n2", host="10.0.0.1", port=9528)))
    )

    assert [s.node.id for s in pool.available()] == ["n2"]
    assert [n["id"] for n in pool.snapshot()["nodes"] if n["drained"]] == ["n1"]


def test_changing_the_node_count_reshards_the_quota_immediately() -> None:
    """加减节点后限流分片要立刻重算，不能等到下一轮探活。"""
    pool = _pool_with("n1")
    seen: list[int] = []
    pool.on_health_change(seen.append)

    _ = pool.replace(
        ClusterConfig(nodes=(Node(id="n1", host="10.0.0.1", port=9528), Node(id="n2", host="10.0.0.1", port=9528)))
    )

    assert seen and seen[-1] == 2
