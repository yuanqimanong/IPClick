from concurrent import futures
import signal
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
    ):
        self._config_path: str | None = config_path
        self.config: Settings = load_config(config_path)

        # 监听地址在这里就定下来，而不是等到 start()。
        #
        # 因为 [TRACE].sqlite_path 与 [LOG].output 支持 {port} 占位符，而占位符
        # 必须替换成**运行时实际生效**的端口（--port 覆盖之后的那个）。日志与
        # 链路记录器都在这个构造函数里初始化，所以端口得比它们更早确定。
        server_section = dict(self.config.get("SERVER", {}))
        self._host: str = host or str(server_section.get("host", "[::]"))
        self._port: int = int(port if port else server_section.get("port", 9527))

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
        self._web_server: WebServer | None = None
        self._web_pages: WebPages | None = None
        self._listen_addr: str = ""
        #: Web 端手动摘除的节点。只在内存里，重启即复原。
        self._drained: set[str] = set()
        #: 仅供 Web 端观测的节点池（探活，不参与任何路由）
        self._node_pool: Any = None

        log.info("IPClickServer initialized")

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

        # 创建gRPC服务器
        self.server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ipclick-worker"),
            # 每个 RPC 都会占用一个 worker 线程做阻塞 IO；不设上限时排队的请求
            # 会在 gRPC 内部无限堆积，直到内存耗尽。
            maximum_concurrent_rpcs=max_workers * 2,
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
                ("grpc.max_concurrent_streams", 100),
                ("grpc.enable_http_proxy", 0),
                # 关掉 SO_REUSEPORT。gRPC 默认开着它（为了多进程分片同一端口），
                # 后果是端口撞了也能"启动成功"：两个进程都在监听，请求被内核
                # 随机分给其中一个。症状是"配置改了一半生效"、"日志只看到一半
                # 请求"，极难定位。本项目不提供多进程分片，所以宁可撞端口时
                # 直接起不来。
                ("grpc.so_reuseport", 0),
            ],
            compression=grpc.Compression.Gzip,
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


def serve(
    config_path: str | None = None,
    host: str | None = None,
    port: int | None = None,
    *,
    web: bool | None = None,
    web_port: int | None = None,
):
    """启动IPClick服务器的便捷函数。

    根据提供的配置路径、主机地址和端口启动服务器。
    如果参数为None，则使用相应的默认值。
    Args:
        config_path (str | None): 自定义配置文件路径。如果为None，则使用默认配置。
        host (str | None): 绑定地址。如果为None，则使用默认地址（如localhost）。
        port (int | None): 服务端口。如果为None，则使用默认端口（如8080）。
    Returns:
        None: 函数执行成功返回None。

    """
    try:
        # host/port 交给构造函数：{port} 占位符要在日志与链路记录初始化之前
        # 拿到最终端口（见 IPClickServer.__init__）
        server = IPClickServer(config_path, web=web, host=host, port=port, web_port=web_port)
        server.start()
    except KeyboardInterrupt:
        pass  # 正常退出
    except Exception as e:
        log.exception(f"Server startup failed: {e}")
        raise


if __name__ == "__main__":
    serve()
