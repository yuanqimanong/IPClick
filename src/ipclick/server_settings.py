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
    host: str = DEFAULT_HOST
    port: int = DEFAULT_GRPC_PORT
    max_workers: int = DEFAULT_MAX_WORKERS
    max_concurrent_rpcs: int = DERIVE
    max_concurrent_streams: int = DERIVE
    processes: int = 1
    compression: str = "gzip"
    async_mode: bool = False

    def __post_init__(self) -> None:
        if self.max_workers < 1:
            raise ConfigError(f"SERVER.max_workers 必须 >= 1，当前为 {self.max_workers}")
        if self.concurrent_rpcs < self.max_workers:
            raise ConfigError(
                f"SERVER.max_concurrent_rpcs({self.concurrent_rpcs}) 不应小于 max_workers({self.max_workers})："
                f"那样线程池永远喂不满，多出来的线程纯属浪费"
            )
        if self.concurrent_streams < 1:
            raise ConfigError(f"SERVER.max_concurrent_streams 必须 >= 1，当前为 {self.concurrent_streams}")

    @property
    def concurrent_rpcs(self) -> int:
        return self.max_concurrent_rpcs or self.max_workers * RPCS_PER_WORKER

    @property
    def concurrent_streams(self) -> int:
        return self.max_concurrent_streams or max(MIN_CONCURRENT_STREAMS, self.concurrent_rpcs)

    @property
    def grpc_compression(self) -> grpc.Compression:
        return _COMPRESSION_ALIASES.get(self.compression, grpc.Compression.Gzip)

    @property
    def listen_addr(self) -> str:
        return f"{self.host}:{self.port}"

    def replace_endpoint(self, host: str | None = None, port: int | None = None) -> ServerSettings:
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
    return sys.platform != "win32" and hasattr(os, "fork")


def resolve_processes(configured: int) -> int:
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
