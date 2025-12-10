# -*- coding:utf-8 -*-

"""
gRPC服务器启动和管理

@time: 2025-12-10
@author: Hades
@file: __init__.py
"""

import logging
import signal
import sys
from concurrent import futures
from typing import Optional

import grpc


def _setup_imports():
    """设置导入路径，解决相对导入问题"""
    import sys
    from pathlib import Path

    # 获取当前文件的目录
    current_dir = Path(__file__).parent
    # 获取项目根目录（src的父目录）
    project_root = current_dir.parent.parent
    # 添加src目录到Python路径
    src_dir = project_root / "src"

    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))


# 在导入其他模块之前设置路径
_setup_imports()

try:
    from ipclick.dto.proto import task_pb2_grpc
    from ipclick.config_loader import load_config
    from ipclick.services import TaskService
    from ipclick.utils.logger import LoggerFactory
    from ipclick.adapters import get_adapter_info, get_default_adapter
except ImportError as e:
    print(f"Import error: {e}")
    print("Please run the server using: python -m ipclick.server")
    sys.exit(1)


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

    def __init__(self, config_path: Optional[str] = None):
        self.config = load_config(config_path)
        self.server = None
        self.task_service = None

        # 配置日志
        LoggerFactory.setup_logging(self.config)
        self.logger = logging.getLogger(__name__)

        self.logger.info("IPClickServer initialized")

    def start(self, port: Optional[int] = None, host: Optional[str] = None) -> None:
        """
        启动服务器

        Args:
            port: 服务端口（覆盖配置）
            host: 绑定地址（覆盖配置）
        """
        server_config = self.config.get('server', {})

        # 参数优先级：函数参数 > 配置文件 > 默认值
        server_port = port or server_config.get('port', 9527)
        server_host = host or server_config.get('host', '0.0.0.0')
        max_workers = server_config.get('max_workers', 10)

        # 创建gRPC服务器
        self.server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=max_workers),
            options=[
                ('grpc.keepalive_time_ms', 30000),
                ('grpc. keepalive_timeout_ms', 5000),
                ('grpc.keepalive_permit_without_calls', True),
                ('grpc.http2.max_pings_without_data', 0),
                ('grpc.http2.min_time_between_pings_ms', 10000),
                ('grpc.http2.min_ping_interval_without_data_ms', 300000)
            ]
        )

        try:
            # 创建任务服务
            self.task_service = TaskService(self.config)

            # 注册服务
            task_pb2_grpc.add_TaskServiceServicer_to_server(self.task_service, self.server)

            # 绑定地址
            listen_addr = f'{server_host}:{server_port}'
            self.server.add_insecure_port(listen_addr)

            # 启动服务器
            self.server.start()

            # 记录启动信息
            self.logger.info(f"IPClick server started on {listen_addr} with {max_workers} workers")
            self._log_startup_info()

            # 注册信号处理
            self._setup_signal_handlers()

            # 等待终止
            try:
                self.server.wait_for_termination()
            except KeyboardInterrupt:
                self.logger.info("Received KeyboardInterrupt, shutting down...")
                self.stop()

        except Exception as e:
            self.logger.error(f"Failed to start server: {e}")
            self.stop()
            raise

    def _log_startup_info(self):
        """记录启动信息"""
        try:
            from .adapters import get_adapter_info, get_default_adapter

            # 记录适配器信息
            adapter_info = get_adapter_info()
            default = get_default_adapter()

            self.logger.info(f"Available HTTP adapters: {list(adapter_info.keys())}")
            self.logger.info(f"Default adapter: {default}")

            # 记录配置信息
            server_config = self.config.get('server', {})
            self.logger.info(f"Server configuration: {server_config}")

            # 记录适配器配置
            adapter_configs = self.config.get('adapters', {})
            if adapter_configs:
                self.logger.info(f"Adapter configurations: {list(adapter_configs.keys())}")

        except Exception as e:
            self.logger.warning(f"Could not log startup info: {e}")

    def _setup_signal_handlers(self):
        """设置信号处理器"""

        def signal_handler(signum, frame):
            signal_name = signal.Signals(signum).name
            self.logger.info(f"Received signal {signal_name} ({signum}), shutting down...")
            self.stop()
            sys.exit(0)

        # 注册信号处理器
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        # Windows支持
        if hasattr(signal, 'SIGBREAK'):
            signal.signal(signal.SIGBREAK, signal_handler)

    def stop(self, grace_period: int = 10):
        """
        停止服务器

        Args:
            grace_period: 优雅停机时间（秒）
        """
        if self.server:
            self.logger.info(f"Stopping gRPC server (grace period: {grace_period}s)...")
            self.server.stop(grace=grace_period)

        if self.task_service:
            self.task_service.cleanup()

        self.logger.info("IPClick server stopped")

    def is_running(self) -> bool:
        """检查服务器是否正在运行"""
        return self.server is not None

    def get_config(self) -> dict:
        """获取服务器配置"""
        return self.config.copy()

    def get_stats(self) -> dict:
        """获取服务器统计信息"""
        stats = {
            'running': self.is_running(),
            'config': self.get_config()
        }

        if self.task_service:
            stats['task_service'] = self.task_service.get_stats()

        return stats


def serve(config_path: Optional[str] = None,
          port: Optional[int] = None,
          host: Optional[str] = None):
    """
    启动IPClick服务器的便捷函数

    Args:
        config_path: 配置文件路径
        port:  服务端口
        host: 绑定地址
    """
    try:
        server = IPClickServer(config_path)
        server.start(port=port, host=host)
    except KeyboardInterrupt:
        pass  # 正常退出
    except Exception as e:
        logging.error(f"Server startup failed: {e}")
        raise


if __name__ == '__main__':
    serve()
