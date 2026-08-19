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
        name = str(dict(server_config).get("compression", "gzip") or "gzip").strip().lower()
        if name in ("none", "off", "no", "identity"):
            return grpc.Compression.NoCompression
        if name == "deflate":
            return grpc.Compression.Deflate
        return grpc.Compression.Gzip

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
        return {"trace": self.recorder.stats()}

    def _cluster_summary(self) -> dict[str, Any]:
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
        pool = getattr(self, "_node_pool", None)
        if pool is None:
            return []
        snapshot = pool.snapshot()
        nodes = list(snapshot.get("nodes") or [])
        for node in nodes:
            node["drained"] = node.get("id") in self._drained
        return nodes

    def _reload_cluster(self) -> tuple[bool, str]:
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
        node_id = form.get("node_id", "").strip()
        if name == "drain" and node_id:
            self._drained.add(node_id)
            return True, f"已手动摘除节点 {node_id}"
        if name == "undrain" and node_id:
            self._drained.discard(node_id)
            return True, f"已恢复节点 {node_id}"
        return False, f"未知操作 {name!r}"

    def start(self, host: str | None = None, port: int | None = None) -> None:
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


def _fork_supported() -> bool:
    return sys.platform != "win32" and hasattr(os, "fork")


def _fork() -> int:
    if sys.platform == "win32":
        raise ConfigError("多进程模式（[SERVER].processes > 1）依赖 os.fork，Windows 不支持；请把 processes 设为 1")
    return os.fork()


def _resolve_processes(config_path: str | None, port: int | None) -> int:
    try:
        config = load_config(config_path, port)
    except Exception:
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
    resolved = max(1, min(8, os.cpu_count() or 1)) if processes == 0 else processes
    if resolved > 1 and not _fork_supported():
        log.warning(
            f"[SERVER].processes 解析为 {resolved}，但多进程模式依赖 os.fork，"
            f"当前平台（{sys.platform}）不支持，已降级为单进程运行"
        )
        return 1
    return resolved


def _probe_port(host: str, port: int) -> None:
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
    resolved_host = host or str(dict(load_config(config_path, port).get("SERVER", {})).get("host", "[::]"))
    resolved_port = int(port or dict(load_config(config_path, port).get("SERVER", {})).get("port", DEFAULT_GRPC_PORT))
    _probe_port(resolved_host, resolved_port)

    children: list[int] = []

    def spawn(index: int) -> int:
        pid = _fork()
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
            except Exception as e:
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
