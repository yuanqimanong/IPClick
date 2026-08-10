from concurrent import futures
import signal
import sys
from types import FrameType
from typing import TypedDict, cast

import grpc
from grpc import Server

from ipclick.auth import AUTH_TOKEN_ENV, TokenAuthInterceptor, load_tokens
from ipclick.config_loader import load_config
from ipclick.dto.proto import task_pb2_grpc
from ipclick.exceptions import ConfigError
from ipclick.services import TaskService
from ipclick.utils.config_util import Settings
from ipclick.utils.log_util import LogUtil, log


class ServerConfig(TypedDict, total=False):
    host: str
    port: int
    max_workers: int


class IPClickServer:
    """
    IPClick gRPC服务器
    """

    def __init__(self, config_path: str | None = None):
        self.config: Settings = load_config(config_path)
        # 按配置里的 [LOG] 节初始化日志（此前这一节完全没被读取过）；
        # [GENERAL].debug 为真时强制 DEBUG 级别
        LogUtil.init_from_config(
            dict(self.config.get("LOG", {})),
            debug=bool(dict(self.config.get("GENERAL", {})).get("debug", False)),
        )
        self.server: Server | None = None
        self.task_service: TaskService | None = None
        log.info("IPClickServer initialized")

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
        tokens = load_tokens(dict(self.config.get("SECURITY", {})))
        auth_interceptor = TokenAuthInterceptor(tokens)

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

            # 绑定地址
            listen_addr = f"{server_host}:{server_port}"
            bound_port: int = self.server.add_insecure_port(listen_addr)
            if bound_port == 0:
                raise RuntimeError(f"Failed to bind to address {listen_addr}")

            # 启动服务器
            self.server.start()

            # 记录启动信息
            log.info(f"IPClick server started on {listen_addr} with {max_workers} workers")
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
        if self.server:
            log.info(f"Stopping gRPC server (grace period: {grace_period}s)...")
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


def serve(config_path: str | None = None, host: str | None = None, port: int | None = None):
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
        server = IPClickServer(config_path)
        server.start(host=host, port=port)
    except KeyboardInterrupt:
        pass  # 正常退出
    except Exception as e:
        log.exception(f"Server startup failed: {e}")
        raise


if __name__ == "__main__":
    serve()
