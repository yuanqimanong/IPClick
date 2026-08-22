"""服务端监听、并发、压缩和进程数量配置。"""

from __future__ import annotations

from dataclasses import dataclass
import os
import sys
from typing import Any, final

import grpc

from ipclick.exceptions import ConfigError
from ipclick.ports import DEFAULT_GRPC_PORT
from ipclick.utils.coerce import as_text, require_bool, require_int
from ipclick.utils.log_util import log


DEFAULT_HOST = "[::]"

# 优雅停机预算。放在这里而不是 server.py：async_server 也要用它，而 server.py
# 是惰性导入 async_server 的，反向在模块层导入会成环。
GRACE_PERIOD_SECONDS = 10

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
            # `port or self.port` 会把显式传进来的 0 当成"没传"；0 是非法端口，
            # 该让 __post_init__ 把它拦下来，而不是伪装成"沿用原值"。
            port=self.port if port is None else port,
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
            # 必须用 require_int：as_int 越界时**静默回落默认值**，于是 port = -1 / 0 在
            # __post_init__ 的 1..65535 校验之前就已经变成 9528 了——config-info 照实显示 -1，
            # 服务端却去绑 9528，等于起了一个没人知道端口的服务。而 port = 70000 因为没给
            # 上界参数、原样穿过去才被校验到，同一项配置两个方向行为不一致。
            port=require_int(config.get("port"), "SERVER.port", defaults.port, minimum=1),
            max_workers=require_int(config.get("max_workers"), "SERVER.max_workers", defaults.max_workers, minimum=1),
            # 与上面 port 同理，这三项也必须用 require_int。as_int 越界或类型不对时静默
            # 回落默认值：processes = "auto" / -1 悄悄变成 1，四进程的吞吐就这么没了，
            # 而 config-info 并不打印 processes，用户没有任何察觉的途径；顺带
            # __post_init__ 里那几个 processes < 0 / streams < 1 的校验也永远走不到。
            max_concurrent_rpcs=require_int(
                config.get("max_concurrent_rpcs"), "SERVER.max_concurrent_rpcs", DERIVE, minimum=DERIVE
            ),
            max_concurrent_streams=require_int(
                config.get("max_concurrent_streams"), "SERVER.max_concurrent_streams", DERIVE, minimum=DERIVE
            ),
            processes=require_int(config.get("processes"), "SERVER.processes", defaults.processes, minimum=0),
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
