"""IPClick 同步/异步、单进程/多进程服务端的统一生命周期入口。"""

from __future__ import annotations

import asyncio
from concurrent import futures
import signal
import sys
from types import FrameType
from typing import Any

import grpc
from grpc import Server

from ipclick.auth import AUTH_TOKEN_ENV, TokenAuthInterceptor, load_tokens
from ipclick.cluster.discovery import create_discovery
from ipclick.cluster.forwarder import ForwardingTaskService, resolve_self_id
from ipclick.cluster.node import ClusterConfig
from ipclick.cluster.pool import NodePool
from ipclick.cluster.tokens import cluster_secret, self_tokens
from ipclick.config_loader import load_config, placeholders
from ipclick.dto.proto import task_pb2_grpc
from ipclick.exceptions import ConfigError
from ipclick.health import HealthReporter
from ipclick.multiprocess import run_workers
from ipclick.protocols import ShardableLimiter
from ipclick.rpc import server_options
from ipclick.secrets import warn_secrets_in_config
from ipclick.server_settings import GRACE_PERIOD_SECONDS, ServerSettings, resolve_processes
from ipclick.services import TaskService
from ipclick.services.async_task_service import AsyncTaskService
from ipclick.tls import TLSSettings, describe, server_credentials, warn_if_insecure
from ipclick.trace import TraceRecorder, TraceSettings, init_recorder
from ipclick.utils.coerce import as_bool
from ipclick.utils.config_util import Settings, section
from ipclick.utils.log_util import LogUtil, log
from ipclick.web import WebConfig, WebCredentials, WebPages, WebServer, announce
from ipclick.web.snapshot import build_dashboard, build_live


FORCED_EXIT_MARGIN_SECONDS = 5

SHUTDOWN_SIGNALS: tuple[str, ...] = ("SIGINT", "SIGTERM", "SIGBREAK")


class IPClickServer:
    """装配配置、鉴权、TLS、任务服务、健康检查和 Web 管理端。"""

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
        verbose: bool = False,
    ):
        self._reuseport: bool = reuseport
        self._config_path: str | None = config_path
        self._cli_port: int | None = port
        self.config: Settings = load_config(config_path, port)

        self.settings: ServerSettings = ServerSettings.from_config(section(self.config, "SERVER")).replace_endpoint(
            host, port
        )

        # verbose 必须参与这一次初始化。CLI 里 `run -v` 先 LogUtil.init(level="DEBUG")，
        # 但这里紧接着按同一个 logger_name 再初始化一次、把 handler 全换成 [LOG].level，
        # 于是 -v 只对建 server 之前那两行生效，之后整个进程都退回 INFO。
        LogUtil.init_from_config(
            placeholders.resolve_for("LOG", section(self.config, "LOG"), self.settings.port),
            debug=verbose or as_bool(section(self.config, "GENERAL").get("debug")),
        )
        self.recorder: TraceRecorder = init_recorder(
            TraceSettings.from_config(
                placeholders.resolve_for("TRACE", section(self.config, "TRACE"), self.settings.port),
                node_id=str(section(self.config, "CLUSTER").get("self_id", "") or ""),
            )
        )
        self.server: Server | None = None
        self.task_service: TaskService | None = None
        self._auth_interceptor: TokenAuthInterceptor | None = None
        self.cluster_config: ClusterConfig = ClusterConfig.from_config(section(self.config, "CLUSTER"))
        self._monitor_config: dict[str, Any] = section(self.config, "MONITOR")
        self.health: HealthReporter = HealthReporter(enabled=self.health_check_enabled)

        self.web_config: WebConfig = WebConfig(section(self.config, "WEB"))
        if web is not None:
            self.web_config.enabled = web
        if web_port:
            self.web_config.port = web_port
        if web_host:
            self.web_config.host = web_host

        self._web_server: WebServer | None = None
        self._web_pages: WebPages | None = None
        self._node_pool: NodePool | None = None
        self.listen_addr: str = ""
        self.drained: set[str] = set()

        log.info("IPClickServer initialized")

    @property
    def health_check_enabled(self) -> bool:
        """返回标准 gRPC 健康接口是否启用。"""
        return as_bool(self._monitor_config.get("health_check"), True)

    @property
    def web_address(self) -> str:
        """返回已启用 Web 管理端的监听地址。"""
        return f"{self.web_config.host}:{self.web_config.port}" if self.web_config.enabled else ""

    @property
    def web_port(self) -> int:
        """返回 Web 管理端口，禁用时为零。"""
        return self.web_config.port if self.web_config.enabled else 0

    def dashboard_extras(self) -> dict[str, Any]:
        """返回由 Web 页面控制器提供的附加仪表盘数据。"""
        return self._web_pages.dashboard_extras() if self._web_pages is not None else {}

    def observed_nodes(self) -> list[dict[str, Any]]:
        """返回 Web 管理端展示的节点快照并合并手动摘除状态。"""
        pool = self._node_pool
        if pool is None:
            return []
        nodes = list(pool.snapshot().get("nodes") or [])
        for node in nodes:
            node["drained"] = node.get("id") in self.drained
        return nodes

    def _start_web(self) -> None:
        if not self.web_config.enabled:
            return
        credentials = WebCredentials.resolve(self.web_config.as_credentials_config())
        self._web_pages = WebPages(
            self.config,
            self.recorder,
            task_service=self.task_service,
            config_path=self._config_path,
            cli_port=self._cli_port,
            runtime_ports={"SERVER.port": self.settings.port, "WEB.port": self.web_config.port},
            on_cluster_changed=self._reload_cluster,
        )
        self._web_server = WebServer(
            lambda: build_dashboard(self),
            credentials,
            action_handler=self._web_action,
            pages=self._web_pages,
            live_provider=lambda: build_live(self),
            theme=self.web_config.theme,
        )
        self._start_node_pool()
        url = self._web_server.start(self.web_config.host, self.web_config.port)
        if url is None:
            self._web_server = None
            return
        announce(credentials, url)

    def _start_node_pool(self) -> None:
        # 幂等：_wire_rate_sharding() 与 _start_web() 都会调这里，不挡住的话第二次会
        # 覆盖 self._node_pool，而**第一个池的探活线程还在跑**——每个节点被探两遍，
        # 且限流分片的回调留在被遗弃的那个池上，Web 端的摘除/恢复作用在新池上却
        # 影响不到限流。stop() 也只停得掉后一个。
        if self._node_pool is not None:
            return
        cluster_config = section(self.config, "CLUSTER")
        try:
            parsed = ClusterConfig.from_config(cluster_config)
            discovery, discovery_config = create_discovery(cluster_config, parsed.nodes)
            if not discovery.resolve():
                return
            self._node_pool = NodePool(
                parsed,
                tls=TLSSettings.from_config(section(self.config, "SECURITY")),
                discovery=discovery,
                discovery_config=discovery_config,
            )
            for node_id in self.drained:
                self._node_pool.drain(node_id)
            log.info(f"Web 端节点观测已启动：{len(self._node_pool)} 个节点")
        except Exception as e:
            log.warning(f"Web 端节点观测未启动：{e}")
            self._node_pool = None

    def _reload_cluster(self) -> tuple[bool, str]:
        reloaded = self._web_pages.config if self._web_pages is not None else self.config
        try:
            updated = ClusterConfig.from_config(section(reloaded, "CLUSTER"))
        except ConfigError as e:
            return False, f"新的集群配置不合法，已保持原样：{e}"

        service = self.task_service
        if isinstance(service, ForwardingTaskService):
            ok, message = service.reload_cluster(reloaded)
            if not ok:
                return ok, message
            self.config = reloaded
            self.cluster_config = updated
            self._refresh_auth_tokens(reloaded, updated)
            return ok, message

        self.config = reloaded
        self.cluster_config = updated
        self._refresh_auth_tokens(reloaded, updated)
        if self._node_pool is not None:
            self._node_pool.stop()
            self._node_pool = None
        self._start_node_pool()
        # 新池不带回调，必须重挂，否则限流分片会冻结在重建前的存活节点数上。
        _ = self._attach_rate_sharding()
        count = len(self._node_pool) if self._node_pool is not None else 0
        return True, f"节点列表已更新（{count} 个节点在探活中）。本进程未开启服务端转发，不参与转发路由"

    def _web_action(self, name: str, form: dict[str, str]) -> tuple[bool, str]:
        node_id = form.get("node_id", "").strip()
        if name == "drain" and node_id:
            self.drained.add(node_id)
            if self._node_pool is not None:
                self._node_pool.drain(node_id)
            if isinstance(self.task_service, ForwardingTaskService):
                self.task_service.set_drained(node_id, True)
            return True, f"已手动摘除节点 {node_id}"
        if name == "undrain" and node_id:
            self.drained.discard(node_id)
            if self._node_pool is not None:
                self._node_pool.undrain(node_id)
            if isinstance(self.task_service, ForwardingTaskService):
                self.task_service.set_drained(node_id, False)
            return True, f"已恢复节点 {node_id}"
        return False, f"未知操作 {name!r}"

    def start(self, host: str | None = None, port: int | None = None) -> None:
        """按当前配置阻塞运行服务，直至停机。"""
        if port is not None and port != self.settings.port:
            log.warning(
                f"start() 传入的端口 {port} 与构造时的 {self.settings.port} 不一致："
                f"[TRACE].sqlite_path / [LOG].output 里的 {{port}} 已按 {self.settings.port} 展开。"
                f"请改用 IPClickServer(port=...) 或 serve(port=...)"
            )
        self.settings = self.settings.replace_endpoint(host, port)

        auth_interceptor, tokens = self._build_auth()
        self._auth_interceptor = auth_interceptor
        tls_settings = TLSSettings.from_config(section(self.config, "SECURITY"))
        warn_secrets_in_config(self.config)

        if self.settings.async_mode:
            self._start_async(auth_interceptor=auth_interceptor, tokens=tokens, tls_settings=tls_settings)
            return
        self._start_sync(auth_interceptor=auth_interceptor, tokens=tokens, tls_settings=tls_settings)

    def _build_auth(self) -> tuple[TokenAuthInterceptor, tuple[str, ...]]:
        """合并外部令牌和绑定本节点的内部令牌。"""
        security_config = section(self.config, "SECURITY")
        cluster_section = section(self.config, "CLUSTER")
        self.cluster_config = ClusterConfig.from_config(cluster_section)

        self_id = resolve_self_id(self.cluster_config, self.settings.host, self.settings.port)
        self_node = self.cluster_config.node_by_id(self_id)
        internal = self_tokens(self_id, self_node.token if self_node else "", cluster_secret(cluster_section))
        tokens = tuple(dict.fromkeys((*load_tokens(security_config), *internal)))
        if internal:
            log.info(f"已接受集群内部令牌（节点 {self_id}）")
        return TokenAuthInterceptor(tokens), tokens

    def _refresh_auth_tokens(self, config: Settings, cluster: ClusterConfig) -> None:
        """使热更新后的节点身份和共享密钥立即作用于入站鉴权。"""
        interceptor = self._auth_interceptor
        if interceptor is None:
            return
        cluster_section = section(config, "CLUSTER")
        self_id = resolve_self_id(cluster, self.settings.host, self.settings.port)
        self_node = cluster.node_by_id(self_id)
        internal = self_tokens(self_id, self_node.token if self_node else "", cluster_secret(cluster_section))
        tokens = tuple(dict.fromkeys((*load_tokens(section(config, "SECURITY")), *internal)))
        interceptor.replace_tokens(tokens)

    def _cluster_kwargs(self, tls_settings: TLSSettings) -> dict[str, Any]:
        return {
            "tls": tls_settings,
            "server_host": self.settings.host,
            "server_port": self.settings.port,
        }

    def _build_task_service(self, tls_settings: TLSSettings) -> TaskService:
        if not self.cluster_config.forwarding_enabled:
            return TaskService(self.config)
        return ForwardingTaskService(self.config, self.cluster_config, **self._cluster_kwargs(tls_settings))

    def _build_async_task_service(self, tls_settings: TLSSettings) -> AsyncTaskService:
        from ipclick.services.async_task_service import AsyncTaskService

        if not self.cluster_config.forwarding_enabled:
            return AsyncTaskService(self.config)

        from ipclick.cluster.async_forwarder import AsyncForwardingTaskService

        return AsyncForwardingTaskService(self.config, self.cluster_config, **self._cluster_kwargs(tls_settings))

    def _start_sync(
        self, *, auth_interceptor: TokenAuthInterceptor, tokens: tuple[str, ...], tls_settings: TLSSettings
    ) -> None:
        """装配并阻塞运行同步 gRPC server。"""
        self.server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=self.settings.max_workers, thread_name_prefix="ipclick-worker"),
            maximum_concurrent_rpcs=self.settings.concurrent_rpcs,
            interceptors=[auth_interceptor],
            options=server_options(max_concurrent_streams=self.settings.concurrent_streams, reuseport=self._reuseport),
            compression=self.settings.grpc_compression,
        )

        try:
            self.task_service = self._build_task_service(tls_settings)
            self._wire_rate_sharding()

            task_pb2_grpc.add_TaskServiceServicer_to_server(self.task_service, self.server)
            self.health.register(self.server)

            self.listen_addr = self.settings.listen_addr
            self._bind(tls_settings)
            self._start_web()

            self.server.start()
            self.health.set_serving()

            log.info(f"IPClick server started on {self.listen_addr} with {self.settings.max_workers} workers")
            self._announce_security(auth_interceptor, tokens, tls_settings)
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

    def _bind(self, tls_settings: TLSSettings) -> None:
        """按 TLS 设置绑定安全或明文监听端口。"""
        if self.server is None:
            raise RuntimeError("gRPC server 尚未创建")
        if tls_settings.enabled:
            bound = self.server.add_secure_port(self.listen_addr, server_credentials(tls_settings))
        else:
            bound = self.server.add_insecure_port(self.listen_addr)
        if bound == 0:
            raise RuntimeError(f"Failed to bind to address {self.listen_addr}")

    def _announce_security(
        self, auth_interceptor: TokenAuthInterceptor, tokens: tuple[str, ...], tls_settings: TLSSettings
    ) -> None:
        log.info(f"传输层：{describe(tls_settings)}")
        warn_if_insecure(tls_settings, self.settings.host)
        if auth_interceptor.enabled:
            log.info(f"已启用令牌鉴权（{len(tokens)} 个有效令牌）")
        else:
            log.warning(
                "未配置鉴权令牌，任何能连到本端口的调用方都可以使用本服务。"
                f"请设置环境变量 {AUTH_TOKEN_ENV} 或配置 [SECURITY].auth_token"
            )

    def _start_async(
        self, *, auth_interceptor: TokenAuthInterceptor, tokens: tuple[str, ...], tls_settings: TLSSettings
    ) -> None:
        """在独立事件循环中运行实验性异步 server。"""
        from ipclick.async_server import serve_async

        service = self._build_async_task_service(tls_settings)
        self.task_service = service
        self._wire_rate_sharding()
        self.listen_addr = self.settings.listen_addr
        self._start_web()

        log.info(f"IPClick server starting on {self.listen_addr}（async_mode，实验性）")
        self._announce_security(auth_interceptor, tokens, tls_settings)
        try:
            asyncio.run(
                serve_async(
                    service,
                    self.listen_addr,
                    credentials=server_credentials(tls_settings) if tls_settings.enabled else None,
                    health_enabled=self.health_check_enabled,
                    max_workers=self.settings.max_workers,
                    max_concurrent_rpcs=self.settings.concurrent_rpcs,
                    max_concurrent_streams=self.settings.concurrent_streams,
                    compression=self.settings.grpc_compression,
                    auth=auth_interceptor,
                    reuseport=self._reuseport,
                )
            )
        except KeyboardInterrupt:
            log.info("Received KeyboardInterrupt, shutting down...")
        finally:
            # serve_async 已异步关闭 service；这里收尾仅由 IPClickServer 持有的资源。
            self.task_service = None
            if self._web_server is not None:
                self._web_server.stop()
                self._web_server = None
            if self._node_pool is not None:
                self._node_pool.stop()
                self._node_pool = None
            self.health.close()
            self.recorder.close()

    def _wire_rate_sharding(self) -> None:
        """按健康节点数动态分摊每 host QPS 配额。"""
        service = self.task_service
        if service is None or self.cluster_config.forwarding_enabled:
            return
        limiters = service.limiters_for_sharding()
        if not limiters or limiters[0].settings.per_host_qps <= 0 or not self.cluster_config.nodes:
            return

        if self._node_pool is None:
            self._start_node_pool()
        if self._node_pool is None:
            log.warning("集群限流分片未启用：节点池起不来，本节点将按完整 per_host_qps 限速")
            return
        if self._attach_rate_sharding(limiters):
            log.info(f"集群限流分片已启用：{len(self.cluster_config.nodes)} 个节点，份额随健康探测变化")

    def _attach_rate_sharding(self, limiters: list[ShardableLimiter] | None = None) -> bool:
        """把当前节点池的健康变化回调挂到限流器上。

        单独拆出来是因为热更新会**重建**节点池：新池不带任何回调，不重挂的话限流器的
        存活节点数会永久冻结在重建前的那一刻——加了节点之后每台仍按旧的（更大的）份额
        发，集群总 QPS 直接超配，而且没有任何报错。
        """
        service = self.task_service
        pool = self._node_pool
        if service is None or pool is None or self.cluster_config.forwarding_enabled:
            return False
        if limiters is None:
            limiters = service.limiters_for_sharding()
        if not limiters or limiters[0].settings.per_host_qps <= 0 or not self.cluster_config.nodes:
            return False
        for limiter in limiters:
            pool.on_health_change(limiter.set_cluster_size)
        return True

    def _setup_signal_handlers(self) -> None:
        def handler(signum: int, _frame: FrameType | None) -> None:
            log.info(f"Received signal {signal.Signals(signum).name} ({signum}), shutting down...")
            self.stop()
            sys.exit(0)

        for name in SHUTDOWN_SIGNALS:
            received = getattr(signal, name, None)
            if received is not None:
                _ = signal.signal(received, handler)

    def stop(self, grace_period: int = GRACE_PERIOD_SECONDS) -> None:
        """先摘除健康状态，再优雅停止 server 并释放所有资源。"""
        if self._web_server is not None:
            self._web_server.stop()
            self._web_server = None
        if self._node_pool is not None:
            self._node_pool.stop()
            self._node_pool = None

        if self.server is not None:
            log.info(f"Stopping gRPC server (grace period: {grace_period}s)...")
            self.health.enter_graceful_shutdown()
            stopped = self.server.stop(grace=grace_period)
            if not stopped.wait(timeout=grace_period + FORCED_EXIT_MARGIN_SECONDS):
                log.warning("部分请求在优雅停机期内未完成，强制退出")
            self.server = None

        if self.task_service is not None:
            self.task_service.cleanup()
            self.task_service = None

        self.health.close()
        self.recorder.close()
        log.info("IPClick server stopped")


def endpoint_settings(config_path: str | None, host: str | None, port: int | None) -> ServerSettings:
    """加载并应用命令行端点覆盖，供单/多进程入口复用。"""
    settings = ServerSettings.from_config(section(load_config(config_path, port), "SERVER"))
    return settings.replace_endpoint(host, port)


def serve(
    config_path: str | None = None,
    host: str | None = None,
    port: int | None = None,
    *,
    web: bool | None = None,
    web_port: int | None = None,
    web_host: str | None = None,
    verbose: bool = False,
) -> None:
    """运行 IPClick server，并按配置选择单进程或 fork worker 模式。"""

    def build(*, enable_web: bool | None, reuseport: bool) -> IPClickServer:
        return IPClickServer(
            config_path,
            web=enable_web,
            host=host,
            port=port,
            web_port=web_port,
            web_host=web_host,
            reuseport=reuseport,
            verbose=verbose,
        )

    try:
        processes = _planned_processes(config_path, host, port)
        if processes > 1:
            run_workers(
                processes,
                endpoint_settings(config_path, host, port),
                lambda index: build(enable_web=web if index == 0 else False, reuseport=True).start(),
            )
            return
        build(enable_web=web, reuseport=False).start()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log.exception(f"Server startup failed: {e}")
        raise


def _planned_processes(config_path: str | None, host: str | None, port: int | None) -> int:
    try:
        return resolve_processes(endpoint_settings(config_path, host, port).processes)
    except Exception:
        return 1


__all__ = ["GRACE_PERIOD_SECONDS", "IPClickServer", "endpoint_settings", "serve"]


if __name__ == "__main__":
    serve()
