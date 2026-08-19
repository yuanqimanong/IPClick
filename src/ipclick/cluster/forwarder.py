"""服务端转发：入口节点把任务分发给集群里的其他节点。

和客户端侧的 :class:`~ipclick.cluster.client.ClusterDownloader` 是**两种**集群
形态，都保留：

* **客户端分发**（``[CLUSTER].forward = "off"``，默认）。调用方自己持有全部节点
  地址，直接连每一台。少一跳、不占入口带宽，适合调用方在内网、能连到所有节点。
* **服务端转发**（``forward = "on"``）。调用方只知道一个地址（比如 A），A 按策略
  挑节点：挑到自己就本地干，挑到别人就把 ``ReqTask`` **原样**转过去，拿到结果再
  回给调用方。适合调用方在集群外、或者不想让调用方感知拓扑。

设计上的几个关键点：

**转发只有一跳。** 转发时带 ``ipclick-forwarded`` metadata；收到带这个标记的
请求一律本地执行，绝不再转。这样"环路"在协议层面就不可能出现，也顺带让**任意
节点都能当入口**——五台机器可以共用完全相同的配置文件，谁被访问谁就是入口。
（对等入口的代价是每台都要能连到其他每台，内网互通的前提下这本来就成立。）

**入口自己也干活。** 只要本节点在 ``nodes`` 列表里，它就参与轮询。不这么做的话
入口机器只剩下转发，白白闲置一台。

**转发的是 protobuf 原文。** 不解开成 DownloadTask 再重新组装——那样每加一个
协议字段都得记着在这里补一遍，漏一个就是静默丢参数。原样转发天然对新字段免疫。

**流式不转发。** ``SendStream`` 永远本地执行：把每个分片再经入口中转一次会让
入口带宽翻倍、还多一跳延迟，而大文件恰恰是最不该这么干的场景。需要让子节点出
流量就直连子节点（客户端分发模式）。
"""

from __future__ import annotations

from collections.abc import Iterator
import threading
from typing import Any, cast, final

import grpc
from grpc import ServicerContext
from typing_extensions import override

from ipclick.auth import build_client_metadata
from ipclick.cluster.node import ClusterConfig, NodeState
from ipclick.cluster.pool import NodePool
from ipclick.cluster.tokens import cluster_secret, token_for, warn_if_missing
from ipclick.cluster.tokens import describe as describe_tokens
from ipclick.compression import CompressionPolicy
from ipclick.dto.models import METHOD_MAP, IPClickAdapter
from ipclick.dto.proto import task_pb2, task_pb2_grpc
from ipclick.exceptions import ConfigError, TransportError
from ipclick.sdk import CHANNEL_OPTIONS
from ipclick.services.task_service import FORWARD_HEADER, TaskService, is_forwarded
from ipclick.tls import TLSSettings, channel_credentials, channel_options
from ipclick.utils.config_util import Settings
from ipclick.utils.log_util import log


#: 转发超时相对任务自身超时的余量（秒）。子节点自己也要跑完整个请求＋重试，
#: 入口的截止时间必须比它宽一点，否则入口先超时、子节点还在白跑。
FORWARD_TIMEOUT_MARGIN = 15.0

#: 自动推算不出任务超时时的兜底转发超时（秒）。
DEFAULT_FORWARD_TIMEOUT = 120.0

#: 浏览器渲染的冷启动与零碎开销余量（秒）。与 browser_adapter 里的两个常量对齐——
#: 转发的截止时间要覆盖子节点真实的预算，否则入口会先放弃、子节点却继续干。
_BROWSER_COLD_START = 60.0
_BROWSER_OVERHEAD = 15.0

#: 走浏览器渲染的适配器名。它们的耗时量级和 HTTP 适配器完全不同。
_BROWSER_ADAPTER_NAMES = frozenset({"browser", "playwright", "patchright", "camoufox", "DrissionPage"})

#: 值得换个节点再试的 gRPC 状态码。
_NODE_FAULT_CODES = frozenset(
    {
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.DEADLINE_EXCEEDED,
        grpc.StatusCode.RESOURCE_EXHAUSTED,
        grpc.StatusCode.INTERNAL,
        grpc.StatusCode.UNKNOWN,
    }
)


@final
class _DirectContext:
    """点名派发到本机时用的假 ServicerContext。

    刻意**不**带转发标记：这一跳就是终点，本来也不会再转。只吞掉状态码设置——
    诊断路径的错误信息在返回的 TaskResp 里，不需要往一条并不存在的 RPC 流上写。
    """

    def set_code(self, _code: object) -> None: ...

    def set_details(self, _details: str) -> None: ...

    def is_active(self) -> bool:
        return True

    def invocation_metadata(self) -> tuple[tuple[str, str], ...]:
        return ()


# 0.7.0 起去掉了 @final。原本标 final 是合理的——那时只有一个转发实现，
# 而它继承 TaskService 已经够绕了。现在多了一个 AsyncForwardingTaskService：
# 它要复用这里全部的路由决策（挑节点、故障转移、防环路、健康计数），只把
# "本地执行那一跳"换成协程。把这些逻辑再抄一份才是更糟的选择——两份路由代码
# 失步的表现是"同步和异步模式挑到了不同的节点"，几乎不可能从现象查回来。
#
# 子类只允许覆写 RPC 入口。碰 _pool / _forward / self_id 这些内部状态之前，
# 先想清楚同步那条路会不会跟着变。
class ForwardingTaskService(TaskService):
    """带服务端转发的 TaskService。

    只覆写 :meth:`Send`。批量（``SendBatch``）在基类里是逐个调 ``Send`` 的，
    所以批量里的任务会自动被分散到各节点上——这正是转发模式最划算的场景。
    """

    def __init__(
        self,
        config: Settings,
        cluster: ClusterConfig | None = None,
        *,
        tls: TLSSettings | None = None,
        pool: NodePool | None = None,
        server_host: str = "",
        server_port: int = 0,
    ):
        super().__init__(config)
        self.cluster: ClusterConfig = cluster or ClusterConfig.from_config(dict(config.get("CLUSTER", {})))
        self._tls: TLSSettings = tls or TLSSettings()
        self._secret: str = cluster_secret(dict(config.get("CLUSTER", {})))

        self.self_id: str = resolve_self_id(self.cluster, server_host, server_port)
        # 链路记录里的"谁执行的"必须和节点列表里的 id 是同一套标识，
        # 否则两边对不上就查不下去。
        if self.self_id:
            self.node_id = self.self_id

        self._pool: NodePool = pool or NodePool(self.cluster, tls=self._tls)
        self._channels: dict[str, grpc.Channel] = {}
        self._channels_lock: threading.Lock = threading.Lock()
        self._compression: CompressionPolicy = CompressionPolicy(dict(config.get("CLIENT", {})))
        self._forwarded_count: int = 0
        self._local_count: int = 0
        # 监听地址要留着：热更新节点列表之后要拿它重新识别"我是哪个节点"
        self._server_host: str = server_host
        self._server_port: int = server_port
        self._reload_lock: threading.Lock = threading.Lock()

        explicit = sum(1 for n in self.cluster.nodes if n.token)
        warn_if_missing(self.self_id, len(self.cluster.nodes), self._secret, explicit)
        log.info(
            f"服务端转发已启用：{len(self.cluster.nodes)} 个节点，策略 {self._pool.balancer.name}，"
            f"本节点 {self.self_id or '未在节点列表中（只转发不执行）'}"
        )
        self_node = self.cluster.node_by_id(self.self_id)
        log.info(f"集群内部鉴权：{describe_tokens(self.self_id, self_node.token if self_node else '', self._secret)}")

    # ------------------------------------------------------------------ #
    # 路由
    # ------------------------------------------------------------------ #

    @override
    def Send(self, request: task_pb2.ReqTask, context: ServicerContext) -> task_pb2.TaskResp:
        """按策略决定本地执行还是转发。"""
        if is_forwarded(context):
            # 已经是转发来的：绝不再转。这是防环路的唯一依据，别加任何例外。
            self._local_count += 1
            return super().Send(request, context)

        tried: set[str] = set()
        attempts = self.cluster.max_failover + 1
        last_error: str = ""

        for attempt in range(attempts):
            try:
                state = self._pool.acquire(exclude=tried)
            except TransportError as e:
                # 没有别的节点可试了——入口自己兜底，别让请求凭空失败。
                last_error = str(e)
                break

            tried.add(state.node.id)
            if state.node.id == self.self_id:
                self._local_count += 1
                state.record_request(success=True)
                return super().Send(request, context)

            try:
                response = self._forward(state, request)
            except grpc.RpcError as e:
                last_error = _rpc_detail(e)
                state.record_request(success=False)
                if _is_node_fault(e):
                    # 节点级故障（连不上、超时、过载）才换人。
                    # 但"换这一次的人"和"把节点摘掉"是两件事——见 _should_mark_unhealthy。
                    if _should_mark_unhealthy(e):
                        state.mark_unhealthy(last_error)
                    log.warning(f"转发到 {state.node.id} 失败（第 {attempt + 1}/{attempts} 次）：{last_error}")
                    continue
                # 参数非法、鉴权失败这类换个节点也是同样结果，直接把错误还给调用方
                log.warning(f"节点 {state.node.id} 拒绝了请求 {request.uuid}：{last_error}")
                _propagate(context, e)
                return self._build_error_response(request, last_error)

            state.record_request(success=True)
            self._forwarded_count += 1
            return response

        # 所有子节点都失败：本地执行。入口自己能干活时，"整个集群挂了"不该
        # 变成"请求失败"——这也是入口默认参与轮询的另一半价值。
        log.warning(f"请求 {request.uuid} 在 {len(tried)} 个节点上转发均失败，改由本节点执行：{last_error}")
        self._local_count += 1
        return super().Send(request, context)

    @override
    def SendStream(self, request: task_pb2.ReqTask, context: ServicerContext) -> Iterator[task_pb2.TaskRespChunk]:
        """流式一律本地执行，理由见模块开头。"""
        yield from super().SendStream(request, context)

    def send_to_node(self, request: task_pb2.ReqTask, node_id: str) -> task_pb2.TaskResp:
        """**点名**把请求发给某个节点，跳过策略选择。

        只给诊断用（Web 端「试一试」的"目标节点"）。刻意不走协议：给 ``ReqTask``
        加一个 ``target_node`` 字段的话，那就成了正式能力，得考虑"点名的节点挂了
        要不要转移"、"点名能不能穿透多跳"这些问题——而这里要的只是"验证我刚加的
        那台机器配对没有"，一条内部路径就够。

        没有故障转移是**有意的**：点名就是点名，转到别的机器上会让这次验证
        失去意义。

        Raises:
            TransportError: 节点 id 不在列表里。
            grpc.RpcError: 目标节点报错或不可达——原样上抛，诊断要看真实错误。
        """
        node = self.cluster.node_by_id(node_id)
        if node is None:
            raise TransportError(f"节点 {node_id!r} 不在集群节点列表里，已有：{[n.id for n in self.cluster.nodes]}")

        if node_id == self.self_id:
            # 点到自己：本地执行。仍然走完整的 Send，这样"点名本机"和"点名别人"
            # 看到的是同一条处理路径。
            self._local_count += 1
            return super().Send(request, cast(ServicerContext, cast(object, _DirectContext())))

        # 用 state_for 而不是在 available() 里找：一个被标成 unhealthy 的节点
        # 恰恰是最需要点名试一次的那个。取不到就现造一个（池子刚换过、还没同步）。
        state = self._pool.state_for(node_id) or NodeState(node)
        try:
            response = self._forward(state, request)
        except grpc.RpcError:
            state.record_request(success=False)
            raise
        state.record_request(success=True)
        self._forwarded_count += 1
        return response

    # ------------------------------------------------------------------ #
    # 热更新
    # ------------------------------------------------------------------ #

    def reload_cluster(self, config: Settings) -> tuple[bool, str]:
        """按新配置原地替换节点列表与路由状态，**不重启进程**。

        0.3 里 ``ClusterConfig`` 和 ``NodePool`` 在构造时就建好存死了，生命周期
        等于进程生命周期：``/nodes`` 页保存完只是改了文件，真正在干活的路由表
        纹丝不动，所以页面上只能写"改完需要重启才生效"。

        能热更新的是**不涉及监听端口**的部分：节点列表、权重、策略、阈值、
        各节点的 token。监听地址属于 gRPC server 的构造参数，那个换不了——
        但它本来也不会因为改节点列表而变。

        Returns:
            ``(是否有变化, 给人看的说明)``。
        """
        section = dict(config.get("CLUSTER", {}))
        with self._reload_lock:
            try:
                updated = ClusterConfig.from_config(section)
            except ConfigError as e:
                return False, f"新的集群配置不合法，已保持原样：{e}"

            previous = {n.id: n for n in self.cluster.nodes}
            self.config = config
            self.cluster = updated
            self._secret = cluster_secret(section)

            added, removed = self._pool.replace(updated)

            # 到被移除节点的连接要关掉，否则它们会一直挂在那儿占着 fd。
            # 地址变了但 id 没变的也要关——继续用旧 channel 就是往老地址发。
            stale = set(removed) | {
                node_id
                for node_id, old in previous.items()
                if (new := updated.node_by_id(node_id)) is not None and new.address != old.address
            }
            self._close_channels(stale)

            # 身份要重新识别：本机可能刚被加进列表（从"只转发"变成"也干活"），
            # 也可能刚被移出去。
            self.self_id = resolve_self_id(updated, self._server_host, self._server_port)
            if self.self_id:
                self.node_id = self.self_id

        if not added and not removed and not stale:
            return True, f"集群配置已热更新（{len(updated.nodes)} 个节点，策略 {self._pool.balancer.name}）"

        parts: list[str] = []
        if added:
            parts.append(f"新增 {', '.join(added)}")
        if removed:
            parts.append(f"移除 {', '.join(removed)}")
        summary = "；".join(parts) or "地址已更新"
        log.info(f"集群配置热更新：{summary}，当前 {len(updated.nodes)} 个节点")
        return True, f"已热更新并立即生效：{summary}"

    def _close_channels(self, node_ids: set[str]) -> None:
        if not node_ids:
            return
        with self._channels_lock:
            for node_id in node_ids:
                channel = self._channels.pop(node_id, None)
                if channel is not None:
                    channel.close()
                    log.debug(f"已关闭到节点 {node_id} 的转发连接")

    # ------------------------------------------------------------------ #
    # 转发
    # ------------------------------------------------------------------ #

    def _forward(self, state: NodeState, request: task_pb2.ReqTask) -> task_pb2.TaskResp:
        """把请求原样转给某个节点，并把它的链路信息接回来。

        Raises:
            grpc.RpcError: 目标节点报错或不可达，由调用方决定换人还是上抛。
        """
        node = state.node
        try:
            adapter_name = IPClickAdapter.from_pb(request.adapter).display_name
        except ValueError:
            adapter_name = "unknown"
        method = METHOD_MAP.get(request.method, "GET")

        with self._recorder.track_request(adapter_name, method, uuid=request.uuid, url=request.url) as tr:
            # 入口这边也留一条记录：Web 端要能看到"经过我的全部流量"。
            # forwarded=True 且 node_id 指向真正的执行者，两条记录（入口的和
            # 子节点的）合起来就是完整链路。
            tr.forwarded = True
            tr.node_id = node.id

            stub = task_pb2_grpc.TaskServiceStub(self._channel_for(node.id, node.host, node.port))
            metadata = ((FORWARD_HEADER, "1"), *build_client_metadata(token_for(node.id, node.token, self._secret)))
            response = stub.Send(
                request,
                timeout=self._timeout_for(request),
                metadata=metadata,
                # 转发这一跳同样可能带着几十 KB 的自动化脚本
                compression=self._compression.for_request(request),
            )

            tr.status_code = response.status_code
            tr.size = len(response.content)
            tr.error = response.error_message
            if response.HasField("trace"):
                # 子节点报的才是真相：适配器可能被解析成具体引擎，重试次数与
                # 排队时间也只有它知道。
                tr.node_id = response.trace.node_id or node.id
                tr.adapter = response.trace.adapter or adapter_name
                tr.attempts = response.trace.attempts or 1
                tr.queued_ms = response.trace.queued_ms
            return response

    def _timeout_for(self, request: task_pb2.ReqTask) -> float:
        """转发这一跳的截止时间。

        必须**不小于子节点自己的预算**，否则入口先超时、子节点还在干活：入口以为
        那台挂了，把它摘掉再换一台重发；被放弃的那台却不会停工，继续跑到自己的
        预算结束，还占着一个浏览器。三个节点轮一遍就是三份重复工作同时压在一台
        机器上——实测过入口 135 秒放弃后子节点又白跑了 161 秒。

        浏览器请求的预算和 HTTP 请求完全不是一个量级（要算页面加载、脚本执行、
        冷启动），所以这里要按适配器类型分别算。
        """
        if self.cluster.forward_timeout > 0:
            return self.cluster.forward_timeout

        base = (
            float(request.timeout_seconds)
            if request.HasField("timeout_seconds") and request.timeout_seconds > 0
            else self.adapter_settings.download_timeout
        )
        if base <= 0:
            return DEFAULT_FORWARD_TIMEOUT

        if self._is_browser_request(request):
            base = max(base, self._browser_budget())
        # 子节点还要重试：把重试次数也算进去，否则一超时就白跑
        retries = request.max_retries if request.HasField("max_retries") else self.adapter_settings.max_attempts
        return base * max(1, retries + 1) + FORWARD_TIMEOUT_MARGIN

    @staticmethod
    def _is_browser_request(request: task_pb2.ReqTask) -> bool:
        try:
            member = IPClickAdapter.from_pb(request.adapter)
        except ValueError:
            return False
        return member.display_name in _BROWSER_ADAPTER_NAMES

    def _browser_budget(self) -> float:
        """子节点渲染一次最多要多久（和 browser_adapter._budget_for 同一套账）。

        取上界即可：这里只是用来把转发的截止时间放宽到"子节点可能真的需要"的
        程度，宁可等久一点，也不要制造出重复渲染。
        """
        s = self.browser_settings
        return s.page_load_timeout + s.script_timeout + _BROWSER_COLD_START + _BROWSER_OVERHEAD

    def _channel_for(self, node_id: str, host: str, port: int) -> grpc.Channel:
        """取（或建）到某节点的 channel，按节点缓存以复用连接。"""
        channel = self._channels.get(node_id)
        if channel is not None:
            return channel
        with self._channels_lock:
            if node_id not in self._channels:
                target = f"{host}:{port}"
                # 复用客户端那份选项：里面有 enable_http_proxy=0。不关掉的话，
                # gRPC 会读环境里的 http_proxy，把节点间的内网转发劫到代理上去
                # （开发机上普遍设了 http_proxy，症状是转发全部 UNAVAILABLE）。
                options = [*CHANNEL_OPTIONS, *channel_options(self._tls)]
                if self._tls.enabled:
                    self._channels[node_id] = grpc.secure_channel(
                        target, channel_credentials(self._tls), options=options
                    )
                else:
                    self._channels[node_id] = grpc.insecure_channel(target, options=options)
                log.debug(f"已建立到节点 {node_id} ({target}) 的转发连接")
            return self._channels[node_id]

    # ------------------------------------------------------------------ #
    # 观测与生命周期
    # ------------------------------------------------------------------ #

    @property
    @override
    def forward_enabled(self) -> bool:
        """给 Ping 用：让探测方看到"这台开着服务端转发"。"""
        return True

    def snapshot(self) -> dict[str, Any]:
        """转发状态，供 Web 端与状态页展示。"""
        data = self._pool.snapshot()
        data.update(
            {
                "forward": True,
                "self_id": self.self_id,
                "self_in_pool": bool(self.cluster.node_by_id(self.self_id)),
                "forwarded_requests": self._forwarded_count,
                "local_requests": self._local_count,
                "internal_auth": bool(self._secret) or any(n.token for n in self.cluster.nodes),
            }
        )
        return data

    @override
    def cleanup(self) -> None:
        self._pool.stop()
        with self._channels_lock:
            for channel in self._channels.values():
                channel.close()
            self._channels.clear()
        super().cleanup()


# --------------------------------------------------------------------------- #
# 身份识别
# --------------------------------------------------------------------------- #


def resolve_self_id(cluster: ClusterConfig, server_host: str = "", server_port: int = 0) -> str:
    """判断本进程对应 ``nodes`` 里的哪一项。

    先看显式配置的 ``self_id``；没配就拿 ``[SERVER]`` 的监听端口与本机地址去比对。
    自动识别是为了让五台机器能共用同一份配置文件——那种情况下 ``self_id``
    只能靠环境变量区分，而端口＋本机 IP 的信息本来就有。

    返回空串表示识别不出来。此时本节点只转发不执行，会打一条警告；即便如此也
    不会成环——转发标记保证第二跳一定本地执行。
    """
    if cluster.self_id:
        if cluster.nodes and cluster.node_by_id(cluster.self_id) is None:
            log.warning(
                f"[CLUSTER].self_id = {cluster.self_id!r} 不在 nodes 列表里，本节点不会参与轮询。"
                f"已有 id：{[n.id for n in cluster.nodes]}"
            )
        return cluster.self_id

    # 没有节点列表就没有"身份"这回事——单机部署不该看到任何集群相关的告警。
    if not cluster.nodes or not server_port:
        return ""

    local = _local_addresses()
    matches = [n.id for n in cluster.nodes if n.port == server_port and n.host.lower() in local]
    if len(matches) == 1:
        log.info(f"自动识别本节点为 {matches[0]}（按监听地址 {server_host}:{server_port} 匹配）")
        return matches[0]
    if len(matches) > 1:
        log.warning(f"有多个节点匹配本机监听地址（{matches}），请显式设置 [CLUSTER].self_id")
        return ""
    # 只在真的会转发时才提"只转发不执行"——转发关着的话这句话是错的，
    # 本节点会照常自己执行所有任务。
    if cluster.forwarding_enabled:
        log.warning(
            f"未能在 nodes 里识别出本节点（监听 {server_host}:{server_port}）。"
            f"本节点将只转发、不执行任务；如需参与轮询请设置 [CLUSTER].self_id"
        )
    else:
        log.debug(f"未能在 nodes 里识别出本节点（监听 {server_host}:{server_port}），但转发未开启，无影响")
    return ""


def _local_addresses() -> set[str]:
    """本机可能被写进节点列表的地址集合。"""
    import socket

    addresses: set[str] = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "[::]", ""}
    try:
        hostname = socket.gethostname()
        addresses.add(hostname.lower())
        # gethostbyname_ex 拿到的是主机名解析出的全部 IPv4，容器与多网卡都能覆盖
        _, _, ips = socket.gethostbyname_ex(hostname)
        addresses.update(ip.lower() for ip in ips)
    except OSError as e:  # pragma: no cover - 取决于本机 DNS 配置
        log.debug(f"取本机地址失败，自动识别节点身份可能不准：{e}")
    return addresses


def _rpc_detail(error: grpc.RpcError) -> str:
    code = getattr(error, "code", lambda: None)()
    details = getattr(error, "details", lambda: "")() or ""
    name = getattr(code, "name", str(code))
    return f"{name}: {details}" if details else str(name)


def _is_node_fault(error: grpc.RpcError) -> bool:
    """这个错误是"节点有问题"（该换人）还是"请求本身有问题"（换人也一样）。

    分类的依据和客户端故障转移一致：只有传输层与资源类错误值得换节点，
    参数非法 / 鉴权失败 / 前置条件不满足换一台还是同样结果，换只会把同一个
    错误重复 N 遍，还拖慢失败反馈。
    """
    code = getattr(error, "code", lambda: None)()
    return code in _NODE_FAULT_CODES


def _should_mark_unhealthy(error: grpc.RpcError) -> bool:
    """这次失败要不要把节点标成 unhealthy（从而影响**后续所有请求**的路由）。

    比 :func:`_is_node_fault` 严格：超时只说明**这一个请求**慢，不说明那台机器坏了。
    一个耗时很长的渲染请求把一台完全健康的节点摘掉，然后流量全压到剩下的机器上、
    让它们也开始超时——这是能把整个集群推倒的正反馈。

    节点健康与否由后台探活（``grpc.health.v1``，连续 N 次才切状态）负责判定，
    那是专门为此设计的通路；请求侧只在"连都连不上"这种铁证下才越过它。
    """
    code = getattr(error, "code", lambda: None)()
    return code is grpc.StatusCode.UNAVAILABLE


def _propagate(context: ServicerContext, error: grpc.RpcError) -> None:
    """把子节点的状态码原样传给调用方。

    不这么做的话，调用方看到的永远是"失败"而分不清是自己参数写错还是集群出问题。
    """
    code = getattr(error, "code", lambda: None)()
    if code is not None:
        context.set_code(code)
    context.set_details(_rpc_detail(error))


__all__ = ["ForwardingTaskService", "resolve_self_id"]
