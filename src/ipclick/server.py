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
from ipclick.adapters.browser_settings import BrowserSettings, resolve_max_pages
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
        self._reuseport: bool = reuseport
        self._config_path: str | None = config_path
        self._cli_port: int | None = port
        self.config: Settings = load_config(config_path, port)

        server_section = dict(self.config.get("SERVER", {}))
        self._host: str = host or str(server_section.get("host", "[::]"))
        self._port: int = int(port if port else server_section.get("port", DEFAULT_GRPC_PORT))

        LogUtil.init_from_config(
            placeholders.resolve_for("LOG", dict(self.config.get("LOG", {})), self._port),
            debug=bool(dict(self.config.get("GENERAL", {})).get("debug", False)),
        )
        self.recorder: TraceRecorder = init_recorder(
            TraceSettings.from_config(
                placeholders.resolve_for("TRACE", dict(self.config.get("TRACE", {})), self._port),
                node_id=str(dict(self.config.get("CLUSTER", {})).get("self_id", "") or ""),
            )
        )
        self.server: Server | None = None
        self.task_service: TaskService | None = None
        self.cluster_config: ClusterConfig = ClusterConfig.from_config(dict(self.config.get("CLUSTER", {})))
        monitor_config = dict(self.config.get("MONITOR", {}))
        self.health: HealthReporter = HealthReporter(enabled=bool(monitor_config.get("health_check", True)))
        self._monitor_config: dict[str, object] = monitor_config

        self.web_config: WebConfig = WebConfig(dict(self.config.get("WEB", {})))
        if web is not None:
            self.web_config.enabled = web
        if web_port:
            self.web_config.port = web_port
        if web_host:
            self.web_config.host = web_host
        self._web_server: WebServer | None = None
        self._web_pages: WebPages | None = None
        self._listen_addr: str = ""
        self._drained: set[str] = set()
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
        self._web_pages = WebPages(
            self.config,
            self.recorder,
            task_service=self.task_service,
            config_path=self._config_path,
            cli_port=self._cli_port,
            runtime_ports={"SERVER.port": self._port, "WEB.port": self.web_config.port},
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
                "grpc_address": f"{self._host}:{self._port}",
                "grpc_port": self._port,
                "web_address": (f"{self.web_config.host}:{self.web_config.port}" if self.web_config.enabled else ""),
                "web_port": self.web_config.port if self.web_config.enabled else 0,
                "version": __version__,
                "mode": mode,
                "node_id": getattr(self.task_service, "node_id", self.recorder.node_id),
                "max_workers": dict(self.config.get("SERVER", {})).get("max_workers", 10),
                "processes": _resolve_processes(self._config_path, self._cli_port),
                "async_mode": _as_strict_bool(dict(self.config.get("SERVER", {})).get("async_mode"), "async_mode"),
                "default_adapter": DEFAULT_ADAPTER_NAME,
                "adapters": sorted(ADAPTER_CLASSES),
                "compression": CompressionPolicy(dict(self.config.get("CLIENT", {}))).describe(),
                "config_path": extras.get("config_path", "—"),
            },
            "trace": extras.get("trace") or self.recorder.stats(),
            "recent": extras.get("recent") or self.recorder.recent(limit=12),
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
                "max_pages_effective": resolve_max_pages(browser.max_pages, engine),
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
        reloaded = self._web_pages.config if self._web_pages is not None else self.config
        self.config = reloaded
        try:
            self.cluster_config = ClusterConfig.from_config(dict(reloaded.get("CLUSTER", {})))
        except ConfigError as e:
            return False, f"新的集群配置不合法，已保持原样：{e}"

        service = self.task_service
        if isinstance(service, ForwardingTaskService):
            return service.reload_cluster(reloaded)

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

        server_host: str = host or self._host
        server_port: int = int(port or self._port)
        if server_port != self._port:
            log.warning(
                f"start() 传入的端口 {server_port} 与构造时的 {self._port} 不一致："
                f"[TRACE].sqlite_path / [LOG].output 里的 {{port}} 已按 {self._port} 展开。"
                f"请改用 IPClickServer(port=...) 或 serve(port=...)"
            )
        self._host, self._port = server_host, server_port
        max_workers: int = int(server_config.get("max_workers", 10))
        if max_workers < 1:
            raise ConfigError(f"SERVER.max_workers 必须 >= 1，当前为 {max_workers}")

        max_concurrent_rpcs: int = int(server_config.get("max_concurrent_rpcs", 0) or 0) or max_workers * 8
        if max_concurrent_rpcs < max_workers:
            raise ConfigError(
                f"SERVER.max_concurrent_rpcs({max_concurrent_rpcs}) 不应小于 max_workers({max_workers})："
                f"那样线程池永远喂不满，多出来的线程纯属浪费"
            )
        max_concurrent_streams: int = int(server_config.get("max_concurrent_streams", 0) or 0) or max(
            100, max_concurrent_rpcs
        )
        if max_concurrent_streams < 1:
            raise ConfigError(f"SERVER.max_concurrent_streams 必须 >= 1，当前为 {max_concurrent_streams}")

        security_config = dict(self.config.get("SECURITY", {}))
        cluster_section = dict(self.config.get("CLUSTER", {}))
        self.cluster_config = ClusterConfig.from_config(cluster_section)
        self_id = resolve_self_id(self.cluster_config, server_host, server_port)
        self_node = self.cluster_config.node_by_id(self_id)
        internal = self_tokens(self_id, self_node.token if self_node else "", cluster_secret(cluster_section))
        tokens = tuple(dict.fromkeys((*load_tokens(security_config), *internal)))
        auth_interceptor = TokenAuthInterceptor(tokens)
        if internal:
            log.info(f"已接受集群内部令牌（节点 {self_id}）")

        tls_settings = TLSSettings.from_config(security_config)

        warn_secrets_in_config(self.config)

        if _as_strict_bool(server_config.get("async_mode"), "async_mode"):
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

        self.server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ipclick-worker"),
            maximum_concurrent_rpcs=max_concurrent_rpcs,
            interceptors=[auth_interceptor],
            options=[
                ("grpc.keepalive_time_ms", 60000),
                ("grpc.keepalive_timeout_ms", 30000),
                ("grpc.keepalive_permit_without_calls", True),
                ("grpc.http2.max_pings_without_data", 2),
                ("grpc.http2.min_time_between_pings_ms", 10000),
                ("grpc.http2.min_ping_interval_without_data_ms", 120000),
                ("grpc.max_send_message_length", 500 * 1024 * 1024),
                ("grpc.max_receive_message_length", 500 * 1024 * 1024),
                ("grpc.max_concurrent_streams", max_concurrent_streams),
                ("grpc.enable_http_proxy", 0),
                ("grpc.so_reuseport", 1 if self._reuseport else 0),
            ],
            compression=self._compression(server_config),
        )

        try:
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

            self._wire_rate_sharding()

            task_pb2_grpc.add_TaskServiceServicer_to_server(self.task_service, self.server)
            self.health.register(self.server)

            listen_addr = f"{server_host}:{server_port}"
            self._listen_addr = listen_addr
            if tls_settings.enabled:
                bound_port: int = self.server.add_secure_port(listen_addr, server_credentials(tls_settings))
            else:
                bound_port = self.server.add_insecure_port(listen_addr)
            if bound_port == 0:
                raise RuntimeError(f"Failed to bind to address {listen_addr}")

            self._start_web()

            self.server.start()
            self.health.set_serving()

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

            self._setup_signal_handlers()

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

        self._wire_rate_sharding()

        listen_addr = f"{server_host}:{server_port}"
        self._listen_addr = listen_addr

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

    def _wire_rate_sharding(self) -> None:
        """客户端分发形态下，把 QPS 按存活节点数分片。

        只在**同时满足**这三条时才做，任何一条不满足都是零开销：

        * ``forward = "off"`` —— 服务端转发时所有任务过入口节点，本来就是全局的
        * 配了 ``[CLUSTER].nodes`` —— 没有集群就没有分片一说
        * 配了 ``per_host_qps`` —— 没限速就不该凭空造出一个限速

        为此会起一个只探活、不参与任何路由的节点池。这是必要成本：份额要跟着
        节点上下线变，就得知道现在活着几台。份额更新挂在探测回调上而不是另起
        定时器——两个独立周期看到的节点状态会错开，而"限流份额"和"路由决策"
        依据的存活数不一致是很难查的一类问题。
        """
        service = self.task_service
        if service is None or self.cluster_config.forwarding_enabled:
            return
        limiters = service.limiters_for_sharding()
        if not limiters or limiters[0].settings.per_host_qps <= 0 or not self.cluster_config.nodes:
            return

        if self._node_pool is None:
            self._start_node_pool()
        pool = self._node_pool
        if pool is None:
            log.warning("集群限流分片未启用：节点池起不来，本节点将按完整 per_host_qps 限速")
            return

        for limiter in limiters:
            pool.on_health_change(limiter.set_cluster_size)
        log.info(f"集群限流分片已启用：{len(self.cluster_config.nodes)} 个节点，份额随健康探测变化")

    def _setup_signal_handlers(self):
        """设置信号处理器"""

        def signal_handler(signum: int, _frame: FrameType | None) -> None:
            signal_name = signal.Signals(signum).name
            log.info(f"Received signal {signal_name} ({signum}), shutting down...")
            self.stop()
            sys.exit(0)

        _ = signal.signal(signal.SIGINT, signal_handler)
        _ = signal.signal(signal.SIGTERM, signal_handler)

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
            self.health.enter_graceful_shutdown()
            stopped = self.server.stop(grace=grace_period)
            if not stopped.wait(timeout=grace_period + 5):
                log.warning("部分请求在优雅停机期内未完成，强制退出")
            self.server = None

        if self.task_service:
            self.task_service.cleanup()
            self.task_service = None

        self.recorder.close()

        log.info("IPClick server stopped")


def _as_strict_bool(raw: object, field: str, default: bool = False) -> bool:
    """按布尔读一个配置项。含糊的值当场报错，不猜。

    不能直接 ``bool()``：TOML 里给布尔值加引号是最常见的笔误之一，而
    ``bool("false")`` 是 **True**。于是 ``async_mode = "false"`` 会**打开**实验性
    异步模式——配置文件上白纸黑字写着关，跑起来却是开的，且没有任何提示。
    人会在一个自以为没开的模式上排查问题，这类事故最费时间。

    能明确判读的字符串照收（从环境变量注入配置时值本来就是字符串），
    真正含糊的才拒绝。
    """
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in ("true", "yes", "on", "1"):
            return True
        if s in ("false", "no", "off", "0", ""):
            return False
        raise ConfigError(f"SERVER.{field} 期望布尔值（true/false），得到 {raw!r}")
    if isinstance(raw, int):
        return bool(raw)
    raise ConfigError(f"SERVER.{field} 期望布尔值（true/false），得到 {raw!r}")


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
        server = IPClickServer(config_path, web=web, host=host, port=port, web_port=web_port, web_host=web_host)
        server.start()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log.exception(f"Server startup failed: {e}")
        raise


if __name__ == "__main__":
    serve()
