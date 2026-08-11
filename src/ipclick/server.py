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
from ipclick.cluster.node import ClusterConfig
from ipclick.cluster.pool import NodePool
from ipclick.config_loader import load_config
from ipclick.dto.proto import task_pb2_grpc
from ipclick.exceptions import ConfigError
from ipclick.factory import resolve_mode
from ipclick.health import HealthReporter
from ipclick.limiter import LimiterSettings
from ipclick.metrics import get_metrics
from ipclick.secrets import warn_secrets_in_config
from ipclick.services import TaskService
from ipclick.tls import TLSSettings, describe, server_credentials, warn_if_insecure
from ipclick.utils.config_util import Settings
from ipclick.utils.log_util import LogUtil, log
from ipclick.web import WebConfig, WebCredentials, WebServer, announce


class ServerConfig(TypedDict, total=False):
    host: str
    port: int
    max_workers: int


class IPClickServer:
    """
    IPClick gRPC服务器
    """

    def __init__(self, config_path: str | None = None, *, web: bool | None = None):
        self.config: Settings = load_config(config_path)
        # 按配置里的 [LOG] 节初始化日志（此前这一节完全没被读取过）；
        # [GENERAL].debug 为真时强制 DEBUG 级别
        LogUtil.init_from_config(
            dict(self.config.get("LOG", {})),
            debug=bool(dict(self.config.get("GENERAL", {})).get("debug", False)),
        )
        self.server: Server | None = None
        self.task_service: TaskService | None = None
        monitor_config = dict(self.config.get("MONITOR", {}))
        self.health: HealthReporter = HealthReporter(enabled=bool(monitor_config.get("health_check", True)))
        self._monitor_config: dict[str, object] = monitor_config

        # Web 管理端：命令行 --web 优先于 [WEB].enabled
        self.web_config: WebConfig = WebConfig(dict(self.config.get("WEB", {})))
        if web is not None:
            self.web_config.enabled = web
        self._web_server: WebServer | None = None
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
        self._web_server = WebServer(
            self._web_snapshot,
            credentials,
            action_handler=self._web_action,
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

        return {
            "server": {
                "address": self._listen_addr,
                "version": __version__,
                "mode": mode,
                "max_workers": dict(self.config.get("SERVER", {})).get("max_workers", 10),
                "default_adapter": DEFAULT_ADAPTER_NAME,
                "adapters": sorted(ADAPTER_CLASSES),
            },
            "security": {
                "tls": describe(TLSSettings.from_config(security)),
                "auth": bool(load_tokens(security)),
                "block_private_networks": security.get("block_private_networks", False),
                "block_metadata_endpoints": security.get("block_metadata_endpoints", True),
            },
            "limits": {
                "per_host_max_concurrent": limits.per_host_max_concurrent,
                "per_host_qps": limits.per_host_qps,
                "backend": str(dict(downloader_cfg.get("rate_limit") or {}).get("backend") or "memory"),
            },
            "browser": {
                "engine": engine,
                "max_pages": browser.max_pages,
                "allow_scripts": browser.allow_scripts,
            },
            "cluster": {"nodes": self._cluster_nodes()},
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

        # 参数优先级：函数参数 > 配置文件 > 默认值
        server_host: str = host or server_config.get("host", "[::]")
        server_port: int = int(port or server_config.get("port", 9527))
        # 注意：这里必须读 max_workers。此前误写成 `port or ...`，
        # 于是 `ipclick run --port 9527` 会创建一个 9527 线程的线程池。
        max_workers: int = int(server_config.get("max_workers", 10))
        if max_workers < 1:
            raise ConfigError(f"SERVER.max_workers 必须 >= 1，当前为 {max_workers}")

        # 鉴权：令牌来自环境变量 IPCLICK_AUTH_TOKEN 或 [SECURITY].auth_token
        security_config = dict(self.config.get("SECURITY", {}))
        tokens = load_tokens(security_config)
        auth_interceptor = TokenAuthInterceptor(tokens)

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
            ],
            compression=grpc.Compression.Gzip,
        )

        try:
            # 创建任务服务
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

            # 指标端点走独立 HTTP 端口（Prometheus 生态惯例），在 gRPC 之前起，
            # 这样即便业务端口起不来也能看到进程状态
            self._start_metrics_server()

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

    def _start_metrics_server(self) -> None:
        """按 [MONITOR] 配置启动 Prometheus 指标端点。"""
        if not bool(self._monitor_config.get("metrics_enabled", False)):
            return
        port = int(self._monitor_config.get("metrics_port", 9528) or 9528)  # pyright: ignore[reportArgumentType]
        host = str(self._monitor_config.get("metrics_host", "0.0.0.0") or "0.0.0.0")
        get_metrics().start_http_server(port, host)

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

        log.info("IPClick server stopped")


def serve(
    config_path: str | None = None,
    host: str | None = None,
    port: int | None = None,
    *,
    web: bool | None = None,
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
        server = IPClickServer(config_path, web=web)
        server.start(host=host, port=port)
    except KeyboardInterrupt:
        pass  # 正常退出
    except Exception as e:
        log.exception(f"Server startup failed: {e}")
        raise


if __name__ == "__main__":
    serve()
