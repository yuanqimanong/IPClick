"""服务端监听、并发、压缩和进程数量配置。"""

from __future__ import annotations

from dataclasses import dataclass
import os
import sys
from typing import Any, final

import grpc

from ipclick.exceptions import ConfigError
from ipclick.ports import DEFAULT_GRPC_PORT
from ipclick.utils.coerce import as_int, as_text, require_bool, require_int
from ipclick.utils.log_util import log


DEFAULT_HOST = "[::]"

DEFAULT_MAX_WORKERS = 100

RPCS_PER_WORKER = 8

MIN_CONCURRENT_STREAMS = 100

MAX_AUTO_PROCESSES = 8

DERIVE = 0

_COMPRESSION_ALIASES: dict[str, grpc.Compression] = {
    "none": grpc.Compression.NoCompression,
    "off": grpc.Compression.NoCompression,
    "no": grpc.Compression.NoCompression,
    "identity": grpc.Compression.NoCompression,
    "deflate": grpc.Compression.Deflate,
    "gzip": grpc.Compression.Gzip,
}


@final
@dataclass(frozen=True)
class ServerSettings:
    """经校验且可派生运行时并发上限的服务端配置。"""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_GRPC_PORT
    max_workers: int = DEFAULT_MAX_WORKERS
    max_concurrent_rpcs: int = DERIVE
    max_concurrent_streams: int = DERIVE
    processes: int = 1
    compression: str = "gzip"
    async_mode: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ConfigError(f"SERVER.port 必须在 1..65535 范围内，当前为 {self.port}")
        if self.max_workers < 1:
            raise ConfigError(f"SERVER.max_workers 必须 >= 1，当前为 {self.max_workers}")
        if self.concurrent_rpcs < self.max_workers:
            raise ConfigError(
                f"SERVER.max_concurrent_rpcs({self.concurrent_rpcs}) 不应小于 max_workers({self.max_workers})："
                f"那样线程池永远喂不满，多出来的线程纯属浪费"
            )
        if self.concurrent_streams < 1:
            raise ConfigError(f"SERVER.max_concurrent_streams 必须 >= 1，当前为 {self.concurrent_streams}")
        if self.processes < 0:
            raise ConfigError(f"SERVER.processes 必须 >= 0，当前为 {self.processes}")

    @property
    def concurrent_rpcs(self) -> int:
        """返回显式 RPC 上限，或按 worker 数推导准入容量。"""
        return self.max_concurrent_rpcs or self.max_workers * RPCS_PER_WORKER

    @property
    def concurrent_streams(self) -> int:
        """返回 HTTP/2 并发流上限，并保证不低于安全基线。"""
        return self.max_concurrent_streams or max(MIN_CONCURRENT_STREAMS, self.concurrent_rpcs)

    @property
    def grpc_compression(self) -> grpc.Compression:
        """将配置别名映射为 gRPC 压缩枚举。"""
        return _COMPRESSION_ALIASES.get(self.compression, grpc.Compression.Gzip)

    @property
    def listen_addr(self) -> str:
        """返回可直接传给 gRPC 的监听地址。"""
        host = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        return f"{host}:{self.port}"

    def replace_endpoint(self, host: str | None = None, port: int | None = None) -> ServerSettings:
        """保留其他设置，仅替换命令行覆盖的监听端点。"""
        if host is None and port is None:
            return self
        return ServerSettings(
            host=host or self.host,
            port=port or self.port,
            max_workers=self.max_workers,
            max_concurrent_rpcs=self.max_concurrent_rpcs,
            max_concurrent_streams=self.max_concurrent_streams,
            processes=self.processes,
            compression=self.compression,
            async_mode=self.async_mode,
        )

    @classmethod
    def from_config(cls, server_config: dict[str, Any] | None) -> ServerSettings:
        """从 ``[SERVER]`` 解析设置，对关键布尔值和 worker 数严格校验。"""
        config = dict(server_config or {})
        defaults = cls()
        return cls(
            host=as_text(config.get("host"), defaults.host),
            port=as_int(config.get("port"), defaults.port, minimum=1),
            max_workers=require_int(config.get("max_workers"), "SERVER.max_workers", defaults.max_workers, minimum=1),
            max_concurrent_rpcs=as_int(config.get("max_concurrent_rpcs"), DERIVE, minimum=DERIVE),
            max_concurrent_streams=as_int(config.get("max_concurrent_streams"), DERIVE, minimum=DERIVE),
            processes=as_int(config.get("processes"), defaults.processes, minimum=0),
            compression=as_text(config.get("compression"), defaults.compression).lower(),
            async_mode=require_bool(config.get("async_mode"), "SERVER.async_mode"),
        )


def fork_supported() -> bool:
    """返回当前平台是否支持项目采用的 fork 多进程模型。"""
    return sys.platform != "win32" and hasattr(os, "fork")


def resolve_processes(configured: int) -> int:
    """解析自动进程数，并在不支持 fork 的平台安全降级。"""
    resolved = max(1, min(MAX_AUTO_PROCESSES, os.cpu_count() or 1)) if configured == 0 else configured
    if resolved > 1 and not fork_supported():
        log.warning(
            f"[SERVER].processes 解析为 {resolved}，但多进程模式依赖 os.fork，"
            f"当前平台（{sys.platform}）不支持，已降级为单进程运行"
        )
        return 1
    return resolved


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_MAX_WORKERS",
    "MAX_AUTO_PROCESSES",
    "MIN_CONCURRENT_STREAMS",
    "RPCS_PER_WORKER",
    "ServerSettings",
    "fork_supported",
    "resolve_processes",
]
