from __future__ import annotations

from typing import Any

import grpc
import pytest

from ipclick.ports import DEFAULT_GRPC_PORT, DEFAULT_WEB_PORT
from ipclick.rpc import client_options, credentials_for, open_channel, open_channel_for, server_options
from ipclick.sdk import unavailable_hint
from ipclick.tls import TLSSettings


def _options(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    return dict(pairs)


def test_client_ping_interval_is_never_rejected_by_our_own_server() -> None:
    client = _options(client_options())
    server = _options(server_options(max_concurrent_streams=100))

    assert client["grpc.keepalive_time_ms"] >= server["grpc.http2.min_ping_interval_without_data_ms"]


def test_idle_channels_may_ping_forever() -> None:
    client = _options(client_options())

    assert client["grpc.keepalive_permit_without_calls"] is True
    assert client["grpc.http2.max_pings_without_data"] == 0


def test_both_sides_agree_on_message_limits() -> None:
    client = _options(client_options())
    server = _options(server_options(max_concurrent_streams=1))

    for key in ("grpc.max_send_message_length", "grpc.max_receive_message_length"):
        assert client[key] == server[key]


def test_environment_proxies_are_ignored_on_both_sides() -> None:
    assert _options(client_options())["grpc.enable_http_proxy"] == 0
    assert _options(server_options(max_concurrent_streams=1))["grpc.enable_http_proxy"] == 0


def test_server_options_pass_through_their_arguments() -> None:
    options = _options(server_options(max_concurrent_streams=321, reuseport=True))
    assert options["grpc.max_concurrent_streams"] == 321
    assert options["grpc.so_reuseport"] == 1
    assert _options(server_options(max_concurrent_streams=1))["grpc.so_reuseport"] == 0


def test_server_name_override_only_applies_with_tls() -> None:
    disabled = TLSSettings(server_name_override="node.internal")
    enabled = TLSSettings(enabled=True, server_name_override="node.internal")

    assert "grpc.ssl_target_name_override" not in _options(client_options(disabled))
    assert _options(client_options(enabled))["grpc.ssl_target_name_override"] == "node.internal"


def test_credentials_are_only_built_when_tls_is_on() -> None:
    assert credentials_for(None) is None
    assert credentials_for(TLSSettings()) is None


def test_open_channel_builds_an_insecure_channel_without_credentials() -> None:
    with open_channel("127.0.0.1:1") as channel:
        assert isinstance(channel, grpc.Channel)


def test_open_channel_for_uses_the_shared_client_options() -> None:
    with open_channel_for("127.0.0.1:1") as channel:
        assert isinstance(channel, grpc.Channel)


def test_keepalive_goaway_gets_its_own_hint() -> None:
    hint = unavailable_hint("Too many pings: too_many_pings", DEFAULT_GRPC_PORT)
    assert "keepalive" in hint
    assert "max_concurrent_rpcs" not in hint


@pytest.mark.parametrize("details", ["refused_stream", "Concurrent RPC limit exceeded"])
def test_admission_control_keeps_the_concurrency_hint(details: str) -> None:
    assert "max_concurrent_rpcs" in unavailable_hint(details, DEFAULT_GRPC_PORT)


def test_plain_unavailable_falls_back_to_the_port_hint() -> None:
    assert "9528" in unavailable_hint("failed to connect", DEFAULT_WEB_PORT)
    assert unavailable_hint("failed to connect", 12345) == ""
