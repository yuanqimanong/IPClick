from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import grpc
from grpc import aio

from ipclick.rpc.options import client_options
from ipclick.tls import TLSSettings, channel_credentials


def credentials_for(tls: TLSSettings | None) -> grpc.ChannelCredentials | None:
    settings = tls or TLSSettings()
    return channel_credentials(settings) if settings.enabled else None


def open_channel(
    target: str,
    *,
    credentials: grpc.ChannelCredentials | None = None,
    options: Sequence[tuple[str, Any]] | None = None,
    compression: grpc.Compression | None = None,
) -> grpc.Channel:
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
    settings = tls or TLSSettings()
    return open_channel(
        target,
        credentials=credentials_for(settings),
        options=client_options(settings),
        compression=compression,
    )


__all__ = ["credentials_for", "open_async_channel", "open_channel", "open_channel_for"]
