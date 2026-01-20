# -*- coding:utf-8 -*-

"""
gRPC服务器启动和管理

@time: 2025-12-10
@author: Hades
@file: __init__.py
"""

from concurrent import futures
import signal
import sys
from typing import cast

import grpc
from grpc import Server

from ipclick.config_loader import load_config
from ipclick.dto.proto import task_pb2_grpc
from ipclick.services import TaskService
from ipclick.utils.config_util import Settings
from ipclick.utils.log_util import log


class IPClickServer:
    """
    IPClick gRPC服务器

    职责：
    1. gRPC服务器的启动和停止
    2. 配置管理
    3. 信号处理
    4. 日志配置
    5. 服务注册
    """

    def __init__(self, config_path: str | None = None):
        self.config: Settings = load_config(config_path)
        self.server: Server | None = None
        self.task_service: None = None
        log.info("IPClickServer initialized")

    def start(self, host: str | None = None, port: int | None = None) -> None:
        """
        启动服务器

        Args:
            port: 服务端口（覆盖配置）
            host: 绑定地址（覆盖配置）
        """
        server_config: Settings = self.config["SERVER"]

        # 参数优先级：函数参数 > 配置文件 > 默认值
        server_host: str = host or server_config["host"] or "[::]"
        server_port: int = port or server_config["port"] or 9527
        max_workers: int = server_config["max_workers"] or 10

        # 创建gRPC服务器
        self.server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=max_workers),
            options=[
                ("grpc.keepalive_time_ms", 60000),
                ("grpc. keepalive_timeout_ms", 30000),
                ("grpc.keepalive_permit_without_calls", True),
                ("grpc.http2.max_pings_without_data", 2),
                ("grpc.http2.min_time_between_pings_ms", 10000),
                ("grpc.http2.min_ping_interval_without_data_ms", 120000),
            ],
        )

        try:
            # 创建任务服务
            self.task_service = TaskService(self.config)

            # 注册服务
            task_pb2_grpc.add_TaskServiceServicer_to_server(self.task_service, self.server)

            # 绑定地址
            listen_addr = f"{server_host}:{server_port}"
            self.server.add_insecure_port(listen_addr)

            # 启动服务器
            self.server.start()

            # 记录启动信息
            log.info(f"IPClick server started on {listen_addr} with {max_workers} workers")

            # 注册信号处理
            self._setup_signal_handlers()

            # 等待终止
            try:
                self.server.wait_for_termination()
            except KeyboardInterrupt:
                log.info("Received KeyboardInterrupt, shutting down...")
                self.stop()

        except Exception as e:
            log.error(f"Failed to start server: {e}")
            self.stop()
            raise

    def _setup_signal_handlers(self):
        """设置信号处理器"""

        def signal_handler(signum, frame):
            signal_name = signal.Signals(signum).name
            log.info(f"Received signal {signal_name} ({signum}), shutting down...")
            self.stop()
            sys.exit(0)

        # 注册信号处理器
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        # Windows支持
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, signal_handler)

    def stop(self, grace_period: int = 10):
        """
        停止服务器

        Args:
            grace_period: 优雅停机时间（秒）
        """
        if self.server:
            log.info(f"Stopping gRPC server (grace period: {grace_period}s)...")
            self.server.stop(grace=grace_period)

        if self.task_service:
            self.task_service.cleanup()

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

    Examples:
        >>> serve()  # 使用默认配置启动
        >>> serve(port=8000)  # 指定端口启动
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
