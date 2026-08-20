"""统一创建同步、异步及 TLS gRPC channel。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import grpc
from grpc import aio

from ipclick.rpc.options import client_options
from ipclick.tls import TLSSettings, channel_credentials


def credentials_for(tls: TLSSettings | None) -> grpc.ChannelCredentials | None:
    """在 TLS 启用时构造 channel 凭据，否则返回 ``None``。"""
    settings = tls or TLSSettings()
    return channel_credentials(settings) if settings.enabled else None


def open_channel(
    target: str,
    *,
    credentials: grpc.ChannelCredentials | None = None,
    options: Sequence[tuple[str, Any]] | None = None,
    compression: grpc.Compression | None = None,
) -> grpc.Channel:
    """按是否提供凭据创建同步 secure/insecure channel。"""
    resolved = list(options if options is not None else client_options())
    if credentials is not None:
        return grpc.secure_channel(target, credentials, options=resolved, compression=compression)
    return grpc.insecure_channel(target, options=resolved, compression=compression)


def open_async_channel(
    target: str,
    *,
    credentials: grpc.ChannelCredentials | None = None,
    options: Sequence[tuple[str, Any]] | None = None,
    compression: grpc.Compression | None = None,
) -> aio.Channel:
    """按是否提供凭据创建异步 secure/insecure channel。"""
    resolved = list(options if options is not None else client_options())
    if credentials is not None:
        return aio.secure_channel(target, credentials, options=resolved, compression=compression)
    return aio.insecure_channel(target, options=resolved, compression=compression)


def open_channel_for(
    target: str,
    tls: TLSSettings | None = None,
    *,
    compression: grpc.Compression | None = None,
) -> grpc.Channel:
    """从 TLS 配置推导凭据和选项后创建同步 channel。"""
    settings = tls or TLSSettings()
    return open_channel(
        target,
        credentials=credentials_for(settings),
        options=client_options(settings),
        compression=compression,
    )


__all__ = ["credentials_for", "open_async_channel", "open_channel", "open_channel_for"]
