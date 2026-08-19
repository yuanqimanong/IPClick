from __future__ import annotations

from typing import Any, Final

from ipclick.tls import TLSSettings, channel_options


MAX_MESSAGE_BYTES: Final = 500 * 1024 * 1024

KEEPALIVE_TIME_MS: Final = 60_000

KEEPALIVE_TIMEOUT_MS: Final = 30_000

MIN_TIME_BETWEEN_PINGS_MS: Final = 10_000

MIN_PING_INTERVAL_WITHOUT_DATA_MS: Final = KEEPALIVE_TIME_MS // 2

UNLIMITED_PINGS_WITHOUT_DATA: Final = 0

NO_HTTP_PROXY: Final[tuple[str, Any]] = ("grpc.enable_http_proxy", 0)


def _message_limits() -> list[tuple[str, Any]]:
    return [
        ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
        ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
    ]


def _keepalive() -> list[tuple[str, Any]]:
    return [
        ("grpc.keepalive_time_ms", KEEPALIVE_TIME_MS),
        ("grpc.keepalive_timeout_ms", KEEPALIVE_TIMEOUT_MS),
        ("grpc.keepalive_permit_without_calls", True),
        ("grpc.http2.max_pings_without_data", UNLIMITED_PINGS_WITHOUT_DATA),
    ]


def client_options(tls: TLSSettings | None = None) -> list[tuple[str, Any]]:
    return [
        *_message_limits(),
        *_keepalive(),
        NO_HTTP_PROXY,
        *channel_options(tls or TLSSettings()),
    ]


def server_options(*, max_concurrent_streams: int, reuseport: bool = False) -> list[tuple[str, Any]]:
    return [
        *_message_limits(),
        *_keepalive(),
        ("grpc.http2.min_time_between_pings_ms", MIN_TIME_BETWEEN_PINGS_MS),
        ("grpc.http2.min_ping_interval_without_data_ms", MIN_PING_INTERVAL_WITHOUT_DATA_MS),
        ("grpc.max_concurrent_streams", max_concurrent_streams),
        NO_HTTP_PROXY,
        ("grpc.so_reuseport", 1 if reuseport else 0),
    ]


__all__ = [
    "KEEPALIVE_TIMEOUT_MS",
    "KEEPALIVE_TIME_MS",
    "MAX_MESSAGE_BYTES",
    "MIN_PING_INTERVAL_WITHOUT_DATA_MS",
    "MIN_TIME_BETWEEN_PINGS_MS",
    "UNLIMITED_PINGS_WITHOUT_DATA",
    "client_options",
    "server_options",
]
