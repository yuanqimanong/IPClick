from concurrent import futures
import contextlib
import os
import signal
import socket
import sys
from types import FrameType
from typing import Any, TypedDict, cast

import grpc
from grpc import Server

from ipclick import __version__
from ipclick.adapters.browser_engines import resolve_engine
from ipclick.adapters.browser_settings import BrowserSettings
from ipclick.auth import AUTH_TOKEN_ENV, TokenAuthInterceptor, load_tokens
from ipclick.cluster.discovery import create_discovery
from ipclick.cluster.forwarder import ForwardingTaskService, resolve_self_id
from ipclick.cluster.node import ClusterConfig
from ipclick.cluster.pool import NodePool
from ipclick.cluster.tokens import cluster_secret, self_tokens
from ipclick.compression import CompressionPolicy
from ipclick.config_loader import load_config, placeholders
from ipclick.dto.proto import task_pb2_grpc
from ipclick.exceptions import ConfigError
from ipclick.factory import resolve_mode
from ipclick.health import HealthReporter
from ipclick.limiter import LimiterSettings
from ipclick.ports import DEFAULT_GRPC_PORT
from ipclick.secrets import warn_secrets_in_config
from ipclick.services import TaskService
from ipclick.tls import TLSSettings, describe, server_credentials, warn_if_insecure
from ipclick.trace import TraceRecorder, TraceSettings, init_recorder
from ipclick.utils.config_util import Settings
from ipclick.utils.log_util import LogUtil, log
from ipclick.web import WebConfig, WebCredentials, WebPages, WebServer, announce


class ServerConfig(TypedDict, total=False):
    host: str
    port: int
    max_workers: int
    max_concurrent_rpcs: int
    max_concurrent_streams: int
    processes: int
    compression: str


class IPClickServer:
    """
    IPClick gRPC服务器
    """

    def __init__(
        self,
        config_path: str | None = None,
        *,
        web: bool | None = None,
        host: str | None = None,
        port: int | None = None,
        web_port: int | None = None,
        web_host: str | None = None,
        reuseport: bool = False,
    ):
        #: 是否允许多个进程共享同一个监听端口（SO_REUSEPORT）。
        #: 只有 serve() 在 [SERVER].processes > 1 时 fork 出来的子进程才置真，
        #: 而且父进程已经独占探测过端口——所以"撞端口静默成功"那个坑仍然堵着。
        self._reuseport: bool = reuseport
        self._config_path: str | None = config_path
        # 带 --port 时先找 ipclick-<端口>.toml：一台机器上起多个实例时，
        # 各读各的配置（不同的 worker 数、限流、链路库）。见 loader.candidate_names。
        self._cli_port: int | None = port
        self.config: Settings = load_config(config_path, port)

        # 监听地址在这里就定下来，而不是等到 start()。
        #
        # 因为 [TRACE].sqlite_path 与 [LOG].output 支持 {port} 占位符，而占位符
        # 必须替换成**运行时实际生效**的端口（--port 覆盖之后的那个）。日志与
        # 链路记录器都在这个构造函数里初始化，所以端口得比它们更早确定。
        server_section = dict(self.config.get("SERVER", {}))
        self._host: str = host or str(server_section.get("host", "[::]"))
        self._port: int = int(port if port else server_section.get("port", DEFAULT_GRPC_PORT))

        # 按配置里的 [LOG] 节初始化日志（此前这一节完全没被读取过）；
        # [GENERAL].debug 为真时强制 DEBUG 级别
        LogUtil.init_from_config(
            placeholders.resolve_for("LOG", dict(self.config.get("LOG", {})), self._port),
            debug=bool(dict(self.config.get("GENERAL", {})).get("debug", False)),
        )
        # 链路记录器必须在 TaskService 之前初始化：后者在构造时就取了单例，
        # 晚一步的话所有埋点都会落到默认（纯内存）实例上，[TRACE] 配置形同虚设。
        self.recorder: TraceRecorder = init_recorder(
            TraceSettings.from_config(
                placeholders.resolve_for("TRACE", dict(self.config.get("TRACE", {})), self._port),
                node_id=str(dict(self.config.get("CLUSTER", {})).get("self_id", "") or ""),
            )
        )
        self.server: Server | None = None
        self.task_service: TaskService | None = None
        #: [CLUSTER] 解析结果。start() 里会重新解析一次（那时才知道最终的监听地址）
        self.cluster_config: ClusterConfig = ClusterConfig.from_config(dict(self.config.get("CLUSTER", {})))
        monitor_config = dict(self.config.get("MONITOR", {}))
        self.health: HealthReporter = HealthReporter(enabled=bool(monitor_config.get("health_check", True)))
        self._monitor_config: dict[str, object] = monitor_config

        # Web 管理端：命令行 --web / --web-port 优先于 [WEB] 里的对应项。
        # 加 --web-port 是因为 gRPC 端口有 --port 而 Web 端没有，同目录起多实例时
        # 第二个的 Web 端会因为端口占用起不来，却只能靠改配置文件绕开。
        self.web_config: WebConfig = WebConfig(dict(self.config.get("WEB", {})))
        if web is not None:
            self.web_config.enabled = web
        if web_port:
            self.web_config.port = web_port
        # --web-host 0.0.0.0 让同一局域网里的手机 / 另一台机器也能打开管理端。
        # 默认仍然只监听 127.0.0.1，改这一项要显式写出来——非回环 + 明文 HTTP
        # 的组合会在 WebServer.start() 里告警。
        if web_host:
            self.web_config.host = web_host
        self._web_server: WebServer | None = None
        self._web_pages: WebPages | None = None
        self._listen_addr: str = ""
        #: Web 端手动摘除的节点。只在内存里，重启即复原。
        self._drained: set[str] = set()
        #: 仅供 Web 端观测的节点池（探活，不参与任何路由）
        self._node_pool: Any = None

        log.info("IPClickServer initialized")

    @staticmethod
    def _compression(server_config: "ServerConfig | dict[str, Any]") -> grpc.Compression:
        """响应压缩方式。

        此前写死 Gzip。对本项目最常见的负载——几百字节到几 KB 的 JSON——压缩
        省下的带宽不值那点 CPU；而在同机或内网部署里，带宽根本不是瓶颈。
        但对大响应体（抓下来的 HTML 页面、下载的文件）它又确实有用，所以不是
        "该关掉"，而是"该能选"。
        """
        name = str(dict(server_config).get("compression", "gzip") or "gzip").strip().lower()
        if name in ("none", "off", "no", "identity"):
            return grpc.Compression.NoCompression
        if name == "deflate":
            return grpc.Compression.Deflate
        return grpc.Compression.Gzip

    def _start_web(self) -> None:
        """按配置启动 Web 管理端。"""
        if not self.web_config.enabled:
            return
        credentials = WebCredentials.resolve(self.web_config.as_credentials_config())
        # 把 TaskService 交给页面层："试一试"要走的是真实的处理路径，
        # 而不是另开一个只在页面上成立的分支。
        self._web_pages = WebPages(
            self.config,
            self.recorder,
            task_service=self.task_service,
            config_path=self._config_path,
            cli_port=self._cli_port,
            # 进程实际在听的两个端口。配置页读的是文件，这两个数只用于在
            # 命令行覆盖过端口时把"文件里写的"和"实际在跑的"同时摆出来。
            runtime_ports={"SERVER.port": self._port, "WEB.port": self.web_config.port},
            # 保存节点之后立即重建路由，不用重启。页面层没有权限碰这些对象，
            # 所以由服务端注入一个回调。
            on_cluster_changed=self._reload_cluster,
        )
        self._web_server = WebServer(
            self._web_snapshot,
            credentials,
            action_handler=self._web_action,
            pages=self._web_pages,
            live_provider=self._live_snapshot,
            theme=self.web_config.theme,
        )
        self._start_node_pool()
        url = self._web_server.start(self.web_config.host, self.web_config.port)
        if url is None:
            self._web_server = None
            return
        announce(credentials, url)

    def _start_node_pool(self) -> None:
        """给 Web 端建一个只读的节点池。

        集群本来是**客户端**的概念，服务端进程不参与路由。但运维想在这台机器上
        确认"我看到的集群拓扑对不对、哪些节点连得上"，为此起一个只探活、不参与
        任何请求分发的池子是值得的。配置里没有节点就什么都不做。
        """
        cluster_config = dict(self.config.get("CLUSTER", {}))
        try:
            parsed = ClusterConfig.from_config(cluster_config)
            discovery, discovery_config = create_discovery(cluster_config, parsed.nodes)
            if not discovery.resolve():
                return
            self._node_pool = NodePool(
                parsed,
                tls=TLSSettings.from_config(dict(self.config.get("SECURITY", {}))),
                discovery=discovery,
                discovery_config=discovery_config,
            )
            log.info(f"Web 端节点观测已启动：{len(self._node_pool)} 个节点")
        except Exception as e:
            # 观测用的东西不该让服务起不来
            log.warning(f"Web 端节点观测未启动：{e}")
            self._node_pool = None

    def _web_snapshot(self) -> dict[str, object]:
        """给 Web 端看的运行状态。

        机密（令牌、代理密码、证书内容）一律只报"有/无"，绝不回显——
        管理界面是个比 gRPC 端口更容易被够到的地方。
        """
        from ipclick.adapters.registry import ADAPTER_CLASSES, DEFAULT_ADAPTER_NAME

        security = dict(self.config.get("SECURITY", {}))
        downloader_cfg = dict(self.config.get("DOWNLOADER", {}))
        limits = LimiterSettings.from_config(downloader_cfg)
        browser = BrowserSettings.from_config(dict(self.config.get("BROWSER", {})))
        try:
            engine = resolve_engine(browser.engine) if browser.enabled else "已关闭"
        except Exception as e:
            engine = f"配置错误: {e}"

        try:
            mode = resolve_mode(self.config)
        except Exception as e:
            mode = f"配置错误: {e}"

        extras: dict[str, Any] = self._web_pages.dashboard_extras() if self._web_pages is not None else {}
        return {
            "server": {
                "address": self._listen_addr,
                # 两个端口分开报，而且都取**运行时实际值**。
                #
                # 0.5.0 之前整个 Web 端一次都没显示过自己在哪个端口上：仪表盘只有
                # 一行没标注的「监听地址」（那是 gRPC 的）。于是人一边看着浏览器
                # 地址栏里的 9527，一边看着页面上的 10086，再加上文档里的 9528，
                # 三个数字凑不出"谁是谁"——这正是"端口有歧义"那条反馈的由来。
                #
                # 必须取 self._port / self.web_config 而不是配置文件里的值：
                # `ipclick run --port X` 时两者不一样，而配置页读的是文件那一份。
                "grpc_address": f"{self._host}:{self._port}",
                "grpc_port": self._port,
                "web_address": (f"{self.web_config.host}:{self.web_config.port}" if self.web_config.enabled else ""),
                "web_port": self.web_config.port if self.web_config.enabled else 0,
                "version": __version__,
                "mode": mode,
                "node_id": getattr(self.task_service, "node_id", self.recorder.node_id),
                "max_workers": dict(self.config.get("SERVER", {})).get("max_workers", 10),
                "default_adapter": DEFAULT_ADAPTER_NAME,
                "adapters": sorted(ADAPTER_CLASSES),
                "compression": CompressionPolicy(dict(self.config.get("CLIENT", {}))).describe(),
                "config_path": extras.get("config_path", "—"),
            },
            "trace": extras.get("trace") or self.recorder.stats(),
            "recent": extras.get("recent") or self.recorder.recent(limit=12),
            # 五个可选 extras 的安装状态。0.3 这里叫 engines 且只有四个"渲染引擎"，
            # niquests 是纯 HTTP 适配器，完全没有展示位。
            "components": extras.get("components") or [],
            "security": {
                "tls": describe(TLSSettings.from_config(security)),
                "auth": bool(load_tokens(security)),
                "block_private_networks": security.get("block_private_networks", False),
                "block_metadata_endpoints": security.get("block_metadata_endpoints", True),
            },
            "limits": {
                "per_host_max_concurrent": limits.per_host_max_concurrent,
                "per_host_qps": limits.per_host_qps,
                "wait_timeout": limits.wait_timeout,
            },
            "browser": {
                "engine": engine,
                "max_pages": browser.max_pages,
                "allow_scripts": browser.allow_scripts,
            },
            "cluster": self._cluster_summary(),
        }

    def _live_snapshot(self) -> dict[str, object]:
        """总览页那一块自动刷新用的**轻量**数据。

        它每 5 秒被拉一次，而完整快照里有一堆按秒计算毫无意义、代价却不小的东西：
        解析 TLS 配置、算集群拓扑、探四个引擎的浏览器本体（那是文件系统扫描）。
        自动刷新的那块只用到链路统计，所以只算这一份。
        """
        return {"trace": self.recorder.stats()}

    def _cluster_summary(self) -> dict[str, Any]:
        """集群展示数据。开了服务端转发时用转发器自己的快照——那才是真正在
        路由的那份状态；否则只报配置里声明的节点及其可达性。
        """
        service = self.task_service
        # 只有 ForwardingTaskService 有 snapshot()；用 isinstance 而不是 getattr
        # 探测，这样类型检查器也能看懂返回值是什么。
        if isinstance(service, ForwardingTaskService):
            data: dict[str, Any] = service.snapshot()
            nodes = cast(list[dict[str, Any]], data.get("nodes") or [])
            for node in nodes:
                node["drained"] = node.get("id") in self._drained
                node["is_self"] = node.get("id") == service.self_id
            return data
        return {
            "forward": False,
            "strategy": self.cluster_config.strategy,
            "self_id": getattr(service, "node_id", ""),
            "internal_auth": bool(cluster_secret(dict(self.config.get("CLUSTER", {})))),
            "nodes": self._cluster_nodes(),
        }

    def _cluster_nodes(self) -> list[dict[str, object]]:
        """服务端进程本身不持有节点池——节点是**客户端**的概念。

        这里展示的是本机配置里声明的节点及其可达性，方便运维确认"这台机器
        看到的集群拓扑对不对"，而不是接管客户端的负载均衡状态。
        """
        pool = getattr(self, "_node_pool", None)
        if pool is None:
            return []
        snapshot = pool.snapshot()
        nodes = list(snapshot.get("nodes") or [])
        for node in nodes:
            node["drained"] = node.get("id") in self._drained
        return nodes

    def _reload_cluster(self) -> tuple[bool, str]:
        """节点列表改完之后原地重建路由。

        0.3 里 ``ClusterConfig`` 与 ``NodePool`` 的生命周期等于进程生命周期：
        ``/nodes`` 保存完只是改了文件，真正在干活的路由表纹丝不动，所以页面上
        只能写"改完需要重启才生效"。现在两条路都重建：

        * 开了服务端转发 → 转发器自己换掉 cluster 与节点池（保留各节点的健康
          计数，否则熔断/恢复的"连续 N 次"判定会被清零）。
        * 没开转发 → 只有那个供 Web 端观测的池子，把它重建一遍。

        监听端口这类要重建 gRPC server 的项**不在**热更新范围内，它们仍然标
        "需重启"——但改节点列表本来也不会动到端口。
        """
        # 页面层已经重新读过文件了，这里跟着换成同一份，免得两边看到的配置不一样
        reloaded = self._web_pages.config if self._web_pages is not None else self.config
        self.config = reloaded
        try:
            self.cluster_config = ClusterConfig.from_config(dict(reloaded.get("CLUSTER", {})))
        except ConfigError as e:
            return False, f"新的集群配置不合法，已保持原样：{e}"

        service = self.task_service
        if isinstance(service, ForwardingTaskService):
            return service.reload_cluster(reloaded)

        # 没开转发：重建观测池。它不参与任何路由，纯粹是让页面上的"这台机器
        # 看到的拓扑"跟得上改动。
        if self._node_pool is not None:
            self._node_pool.stop()
            self._node_pool = None
        self._start_node_pool()
        count = len(self._node_pool) if self._node_pool is not None else 0
        return True, f"节点列表已更新（{count} 个节点在探活中）。本进程未开启服务端转发，不参与转发路由"

    def _web_action(self, name: str, form: dict[str, str]) -> tuple[bool, str]:
        """处理 Web 端的可变操作。

        只接受运行时的、可逆的操作。改配置一律不做——这个服务能代任意 URL
        发请求，一个能改它配置的网页就是极高价值的目标。
        """
        node_id = form.get("node_id", "").strip()
        if name == "drain" and node_id:
            self._drained.add(node_id)
            return True, f"已手动摘除节点 {node_id}"
        if name == "undrain" and node_id:
            self._drained.discard(node_id)
            return True, f"已恢复节点 {node_id}"
        return False, f"未知操作 {name!r}"

    def start(self, host: str | None = None, port: int | None = None) -> None:
        """
        启动服务器

        Args:
            port: 服务端口（覆盖配置）
            host: 绑定地址（覆盖配置）
        """
        server_config: ServerConfig = cast(ServerConfig, self.config.get("SERVER", {}))

        # 参数优先级：函数参数 > 构造时确定的值（已含构造参数与配置文件）> 默认值。
        # 正常路径上 serve() 会把 host/port 传给构造函数，这里两次算出来的是同一个值。
        server_host: str = host or self._host
        server_port: int = int(port or self._port)
        if server_port != self._port:
            # 走到这里说明调用方绕过构造函数、只在 start() 里给了端口。日志与链路
            # 记录已经按旧端口初始化过了，{port} 占位符会指向另一个文件——与其
            # 让人事后纳闷"日志怎么跑到别的文件里去了"，不如当场说清楚。
            log.warning(
                f"start() 传入的端口 {server_port} 与构造时的 {self._port} 不一致："
                f"[TRACE].sqlite_path / [LOG].output 里的 {{port}} 已按 {self._port} 展开。"
                f"请改用 IPClickServer(port=...) 或 serve(port=...)"
            )
        self._host, self._port = server_host, server_port
        # 注意：这里必须读 max_workers。此前误写成 `port or ...`，
        # 于是 `ipclick run --port 9527` 会创建一个 9527 线程的线程池。
        max_workers: int = int(server_config.get("max_workers", 10))
        if max_workers < 1:
            raise ConfigError(f"SERVER.max_workers 必须 >= 1，当前为 {max_workers}")

        # 并发准入上限。此前写死成 max_workers * 2，于是"能同时在途多少个 RPC"
        # 被"线程池多大"决定了——这两件事其实无关：
        #
        #   * 线程数决定的是**同时能干多少活**（每个阻塞 IO 占一个线程）
        #   * 准入上限决定的是**排队能排多长**（超了就拒流）
        #
        # 绑在一起的后果实测很难看：默认 max_workers=100 → 上限 200，客户端
        # 500 并发时服务端直接回 RST_STREAM(REFUSED_STREAM)，SDK 按 UNAVAILABLE
        # 重试两次仍拒，成功率掉到 68.7%。而那时服务端 CPU 只用了 1.45 个核——
        # 它不是忙不过来，是自己把门关上了。
        #
        # 改成可配置，默认给到 max_workers * 8：排队要有上限（不然内存无界），
        # 但这个上限该按"愿意让调用方等多久"来定，而不是按线程数。
        max_concurrent_rpcs: int = int(server_config.get("max_concurrent_rpcs", 0) or 0) or max_workers * 8
        if max_concurrent_rpcs < max_workers:
            raise ConfigError(
                f"SERVER.max_concurrent_rpcs({max_concurrent_rpcs}) 不应小于 max_workers({max_workers})："
                f"那样线程池永远喂不满，多出来的线程纯属浪费"
            )
        # 单条 HTTP/2 连接上的并发流上限。SDK 默认一个 Downloader 一条 channel，
        # 也就是一条 TCP 连接——这个值直接成了单客户端的并发天花板。写死 100
        # 意味着不管服务端多空闲，一个客户端最多只能有 100 个请求在途。
        max_concurrent_streams: int = int(server_config.get("max_concurrent_streams", 0) or 0) or max(
            100, max_concurrent_rpcs
        )

        # 鉴权：令牌来自环境变量 IPCLICK_AUTH_TOKEN 或 [SECURITY].auth_token
        security_config = dict(self.config.get("SECURITY", {}))
        cluster_section = dict(self.config.get("CLUSTER", {}))
        self.cluster_config = ClusterConfig.from_config(cluster_section)
        # 集群内部令牌：本节点接受由共享密钥派生出的、属于自己的那一个。
        # 这样五台机器可以共用同一份配置 + 同一个 .env，各自算出的令牌却互不相同。
        self_id = resolve_self_id(self.cluster_config, server_host, server_port)
        self_node = self.cluster_config.node_by_id(self_id)
        internal = self_tokens(self_id, self_node.token if self_node else "", cluster_secret(cluster_section))
        tokens = tuple(dict.fromkeys((*load_tokens(security_config), *internal)))
        auth_interceptor = TokenAuthInterceptor(tokens)
        if internal:
            log.info(f"已接受集群内部令牌（节点 {self_id}）")

        # 传输层加密。证书读取与组合校验在这里就做掉——带着半套 TLS 配置起来，
        # 比起不来危险得多。
        tls_settings = TLSSettings.from_config(security_config)

        # 机密写在配置文件里会跟着进版本库/备份/日志，启动时点一下名。
        # 仍然照常生效，[SECURITY].allow_secrets_in_config = true 可关掉提示。
        warn_secrets_in_config(self.config)

        # [SERVER].async_mode：换成 grpc.aio 服务端。默认关，见 async_server 的模块注释。
        if bool(server_config.get("async_mode", False)):
            self._start_async(
                server_host=server_host,
                server_port=server_port,
                max_workers=max_workers,
                max_concurrent_rpcs=max_concurrent_rpcs,
                max_concurrent_streams=max_concurrent_streams,
                auth_interceptor=auth_interceptor,
                tls_settings=tls_settings,
                server_config=server_config,
            )
            return

        # 创建gRPC服务器
        self.server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ipclick-worker"),
            # 每个 RPC 都会占用一个 worker 线程做阻塞 IO；不设上限时排队的请求
            # 会在 gRPC 内部无限堆积，直到内存耗尽。上限本身仍然要有，只是不该
            # 由线程数推导——见上面 max_concurrent_rpcs 的说明。
            maximum_concurrent_rpcs=max_concurrent_rpcs,
            interceptors=[auth_interceptor],
            options=[
                ("grpc.keepalive_time_ms", 60000),
                ("grpc.keepalive_timeout_ms", 30000),
                ("grpc.keepalive_permit_without_calls", True),
                ("grpc.http2.max_pings_without_data", 2),
                ("grpc.http2.min_time_between_pings_ms", 10000),
                ("grpc.http2.min_ping_interval_without_data_ms", 120000),
                ("grpc.max_send_message_length", 500 * 1024 * 1024),  # 500MB
                ("grpc.max_receive_message_length", 500 * 1024 * 1024),
                ("grpc.max_concurrent_streams", max_concurrent_streams),
                ("grpc.enable_http_proxy", 0),
                # SO_REUSEPORT：只有在多进程分片时才打开。
                #
                # 关掉它的理由仍然成立——端口撞了也能"启动成功"，两个进程都在
                # 监听、请求被内核随机分走，症状是"配置改了一半生效"、"日志只看到
                # 一半请求"，极难定位。所以单进程模式下继续关死。
                #
                # 但把它一律关死也堵掉了 Python gRPC 服务端唯一的横向扩展路径。
                # 这个服务端是一请求一线程的 CPython 进程，实测 8 个核只能用出
                # 1.45 个——GIL 才是天花板，加线程、加内存、换更快的机器都没用，
                # 只有多进程能突破。所以改成：processes > 1 时打开，并且由父进程
                # 先独占探测一次端口（见 _probe_port），撞端口照样起不来。
                ("grpc.so_reuseport", 1 if self._reuseport else 0),
            ],
            compression=self._compression(server_config),
        )

        try:
            # 创建任务服务。开了服务端转发就换成会分发的那个子类——它只覆写
            # Send，本地执行路径与单机完全一致。
            if self.cluster_config.forwarding_enabled:
                self.task_service = ForwardingTaskService(
                    self.config,
                    self.cluster_config,
                    tls=tls_settings,
                    server_host=server_host,
                    server_port=server_port,
                )
            else:
                self.task_service = TaskService(self.config)

            # 注册服务
            task_pb2_grpc.add_TaskServiceServicer_to_server(self.task_service, self.server)
            self.health.register(self.server)

            # 绑定地址。TLS 凭据在绑定之前构造：证书有问题就该在这里失败，
            # 而不是等到第一个客户端连上来才发现。
            listen_addr = f"{server_host}:{server_port}"
            self._listen_addr = listen_addr
            if tls_settings.enabled:
                bound_port: int = self.server.add_secure_port(listen_addr, server_credentials(tls_settings))
            else:
                bound_port = self.server.add_insecure_port(listen_addr)
            if bound_port == 0:
                raise RuntimeError(f"Failed to bind to address {listen_addr}")

            # Web 管理端在 gRPC 之前起：起不来只是少个界面，不该让整个服务失败，
            # 但要让人看到那条错误。
            self._start_web()

            # 启动服务器
            self.server.start()
            # 只有真正 start() 之后才宣告可服务，避免上游在还没监听时就打流量进来
            self.health.set_serving()

            # 记录启动信息
            log.info(f"IPClick server started on {listen_addr} with {max_workers} workers")
            log.info(f"传输层：{describe(tls_settings)}")
            warn_if_insecure(tls_settings, server_host)
            if auth_interceptor.enabled:
                log.info(f"已启用令牌鉴权（{len(tokens)} 个有效令牌）")
            else:
                log.warning(
                    "未配置鉴权令牌，任何能连到本端口的调用方都可以使用本服务。"
                    f"请设置环境变量 {AUTH_TOKEN_ENV} 或配置 [SECURITY].auth_token"
                )

            # 注册信号处理
            self._setup_signal_handlers()

            # 等待终止
            try:
                _ = self.server.wait_for_termination()
            except KeyboardInterrupt:
                log.info("Received KeyboardInterrupt, shutting down...")
                self.stop()

        except Exception as e:
            log.exception(f"Failed to start server: {e}")
            self.stop()
            raise

    def _start_async(
        self,
        *,
        server_host: str,
        server_port: int,
        max_workers: int,
        max_concurrent_rpcs: int,
        max_concurrent_streams: int,
        auth_interceptor: TokenAuthInterceptor,
        tls_settings: TLSSettings,
        server_config: Any,
    ) -> None:
        """异步模式的启动路径。

        刻意不复用同步那一大段：aio 的 server 生命周期是协程（start / stop /
        wait_for_termination 都要 await），硬塞进同步流程只会让两边都别扭。
        共用的是**参数解析**——上面算出来的 max_workers、准入上限、压缩、
        鉴权、TLS 全部原样传进去，所以两种模式对同一份配置的理解完全一致。
        """
        import asyncio as _asyncio

        from ipclick.async_server import serve_async
        from ipclick.services.async_task_service import AsyncTaskService

        if self.cluster_config.forwarding_enabled:
            # 转发的**路由决策**（挑节点、故障转移、防环路）复用同步实现，
            # 只有"本地执行"那一跳走异步。出站那一跳仍是同步 stub、仍占线程，
            # 取舍写在 async_forwarder 的模块注释里。
            from ipclick.cluster.async_forwarder import AsyncForwardingTaskService

            self.task_service = AsyncForwardingTaskService(
                self.config,
                self.cluster_config,
                tls=tls_settings,
                server_host=server_host,
                server_port=server_port,
            )
        else:
            self.task_service = AsyncTaskService(self.config)

        listen_addr = f"{server_host}:{server_port}"
        self._listen_addr = listen_addr

        # Web 管理端照常起。「试一试」走 AsyncTaskService.send_from_thread：
        # 它跑在 HTTP 工作线程里，用 run_coroutine_threadsafe 把协程投递回服务端的
        # 事件循环，并**带超时**——循环若被卡住，不能让 HTTP 工作线程无限期占着，
        # 那会把管理端一起拖挂，而人看到的只是页面转圈。
        self._start_web()

        log.info(f"IPClick server starting on {listen_addr}（async_mode，实验性）")
        warn_if_insecure(tls_settings, server_host)
        try:
            _asyncio.run(
                serve_async(
                    self.task_service,
                    listen_addr,
                    credentials=server_credentials(tls_settings) if tls_settings.enabled else None,
                    health_enabled=bool(self._monitor_config.get("health_check", True)),
                    max_workers=max_workers,
                    max_concurrent_rpcs=max_concurrent_rpcs,
                    max_concurrent_streams=max_concurrent_streams,
                    compression=self._compression(server_config),
                    auth=auth_interceptor,
                    reuseport=self._reuseport,
                )
            )
        except KeyboardInterrupt:
            log.info("Received KeyboardInterrupt, shutting down...")

    def _setup_signal_handlers(self):
        """设置信号处理器"""

        def signal_handler(signum: int, _frame: FrameType | None) -> None:
            signal_name = signal.Signals(signum).name
            log.info(f"Received signal {signal_name} ({signum}), shutting down...")
            self.stop()
            sys.exit(0)

        # 注册信号处理器
        _ = signal.signal(signal.SIGINT, signal_handler)
        _ = signal.signal(signal.SIGTERM, signal_handler)

        # Windows支持
        if hasattr(signal, "SIGBREAK"):
            _ = signal.signal(signal.SIGBREAK, signal_handler)

    def stop(self, grace_period: int = 10):
        """
        停止服务器

        Args:
            grace_period: 优雅停机时间（秒）
        """
        if self._web_server is not None:
            self._web_server.stop()
            self._web_server = None
        if self._node_pool is not None:
            self._node_pool.stop()
            self._node_pool = None
        if self.server:
            log.info(f"Stopping gRPC server (grace period: {grace_period}s)...")
            # 先把健康状态置为 NOT_SERVING 再 stop：上游负载均衡器据此摘掉本节点，
            # 在途请求还能在优雅期内跑完。反过来做的话是先掐连接、上游才后知后觉。
            self.health.enter_graceful_shutdown()
            # server.stop() 立即返回一个 Event，必须 wait 才算真的优雅停机；
            # 原来没等就往下走并 sys.exit(0)，在途请求会被直接掐断。
            stopped = self.server.stop(grace=grace_period)
            if not stopped.wait(timeout=grace_period + 5):
                log.warning("部分请求在优雅停机期内未完成，强制退出")
            self.server = None

        if self.task_service:
            self.task_service.cleanup()
            self.task_service = None

        # 最后关：前面的清理动作本身也可能落链路记录
        self.recorder.close()

        log.info("IPClick server stopped")


def _resolve_processes(config_path: str | None, port: int | None) -> int:
    """读 ``[SERVER].processes``。

    ``0`` 表示按 CPU 核数自动决定（上限 8——再多的收益会被目标站点和内核
    的连接开销吃掉，而每个进程都要一份完整的适配器和连接池）。
    """
    try:
        config = load_config(config_path, port)
    except Exception:  # pragma: no cover - 配置有问题的话交给正常路径去报错
        return 1
    raw = dict(config.get("SERVER", {})).get("processes", 1)
    # bool 要在 int 之前挡掉：TOML 里写 processes = false 是"别开多进程"的意思，
    # 但 int(False) == 0，而 0 恰好被定义成"按 CPU 核数自动"——于是关掉它反而
    # 会起满 8 个进程。这类"写 false 得到最大值"的坑一旦漏出去极难被发现。
    if isinstance(raw, bool):
        return 1
    try:
        processes = int(raw)
    except (TypeError, ValueError):
        return 1
    if processes < 0:
        return 1
    if processes == 0:
        return max(1, min(8, os.cpu_count() or 1))
    return processes


def _probe_port(host: str, port: int) -> None:
    """在 fork 之前独占地试绑一次端口。

    多进程分片要打开 SO_REUSEPORT，而那正好让"端口已被别的程序占用"变成
    静默成功——两个不相干的进程一起监听，请求被内核随便分。所以在放开
    SO_REUSEPORT 之前，先用一个**不带** SO_REUSEPORT 的普通 socket 试绑：
    绑不上就当场失败，和单进程模式的行为保持一致。
    """
    family = socket.AF_INET6 if ":" in host.strip("[]") or host in ("[::]", "::") else socket.AF_INET
    bind_host = host.strip("[]") if family is socket.AF_INET6 else host
    if bind_host in ("", "*"):
        bind_host = "::" if family is socket.AF_INET6 else "0.0.0.0"
    probe = socket.socket(family, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((bind_host, port))
    except OSError as e:
        raise RuntimeError(
            f"端口 {host}:{port} 无法绑定（{e}）。多进程模式会打开 SO_REUSEPORT，"
            f"那会让端口冲突变成静默成功，所以这里先独占探测一次"
        ) from e
    finally:
        probe.close()


def _serve_multiprocess(
    processes: int,
    *,
    config_path: str | None,
    host: str | None,
    port: int | None,
    web: bool | None,
    web_port: int | None,
    web_host: str | None,
) -> None:
    """多进程分片：N 个子进程靠 SO_REUSEPORT 共享同一个监听端口。

    为什么需要它：服务端是一请求一线程的 CPython 进程，实测 16 核机器上
    8 个核只能用出 1.45 个——GIL 是天花板，`max_workers` 从 32 调到 256
    对吞吐没有任何影响（实测 279.8 / 277.9 QPS，差异在噪声内）。唯一能
    真正用上多核的办法就是多进程。

    分发由**内核**做（SO_REUSEPORT 按四元组哈希），不需要任何中间件，
    也不需要改客户端——对调用方来说仍然只有一个地址一个端口。

    Web 管理端只在 0 号子进程里起：它是有状态的（会话、安装任务），
    起 N 份会让登录态随机失效，而且 N 个进程抢同一个 Web 端口必然失败。
    """
    resolved_host = host or str(dict(load_config(config_path, port).get("SERVER", {})).get("host", "[::]"))
    resolved_port = int(port or dict(load_config(config_path, port).get("SERVER", {})).get("port", DEFAULT_GRPC_PORT))
    _probe_port(resolved_host, resolved_port)

    children: list[int] = []

    def spawn(index: int) -> int:
        pid = os.fork()
        if pid == 0:
            # 子进程：只有 0 号带 Web 管理端
            try:
                server = IPClickServer(
                    config_path,
                    web=web if index == 0 else False,
                    host=host,
                    port=port,
                    web_port=web_port,
                    web_host=web_host,
                    reuseport=True,
                )
                server.start()
            except KeyboardInterrupt:
                pass
            except Exception as e:  # pragma: no cover
                log.exception(f"worker {index} 退出: {e}")
                os._exit(1)
            os._exit(0)
        return pid

    for index in range(processes):
        children.append(spawn(index))

    log.info(f"IPClick 多进程模式：{processes} 个 worker 共享 {resolved_host}:{resolved_port}（SO_REUSEPORT）")

    def forward(signum: int, _frame: FrameType | None) -> None:
        for pid in children:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signum)

    _ = signal.signal(signal.SIGINT, forward)
    _ = signal.signal(signal.SIGTERM, forward)

    for pid in children:
        with contextlib.suppress(ChildProcessError, InterruptedError):
            _ = os.waitpid(pid, 0)


def serve(
    config_path: str | None = None,
    host: str | None = None,
    port: int | None = None,
    *,
    web: bool | None = None,
    web_port: int | None = None,
    web_host: str | None = None,
):
    """启动IPClick服务器的便捷函数。

    根据提供的配置路径、主机地址和端口启动服务器。
    如果参数为None，则使用相应的默认值。
    Args:
        config_path (str | None): 自定义配置文件路径。如果为None，则使用默认配置。
        host (str | None): 绑定地址。如果为None，则使用默认地址（如localhost）。
        port (int | None): 服务端口。如果为None，则使用默认端口（如8080）。
        web_host (str | None): Web 管理端的绑定地址（覆盖 ``[WEB].host``）。
            传 ``0.0.0.0`` 让同一局域网内的其他设备也能访问。
    Returns:
        None: 函数执行成功返回None。

    """
    try:
        processes = _resolve_processes(config_path, port)
        if processes > 1:
            _serve_multiprocess(
                processes,
                config_path=config_path,
                host=host,
                port=port,
                web=web,
                web_port=web_port,
                web_host=web_host,
            )
            return
        # host/port 交给构造函数：{port} 占位符要在日志与链路记录初始化之前
        # 拿到最终端口（见 IPClickServer.__init__）
        server = IPClickServer(config_path, web=web, host=host, port=port, web_port=web_port, web_host=web_host)
        server.start()
    except KeyboardInterrupt:
        pass  # 正常退出
    except Exception as e:
        log.exception(f"Server startup failed: {e}")
        raise


if __name__ == "__main__":
    serve()
