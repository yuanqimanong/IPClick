from __future__ import annotations

import grpc
import pytest

from ipclick import server_settings
from ipclick.exceptions import ConfigError
from ipclick.ports import DEFAULT_GRPC_PORT
from ipclick.server_settings import MAX_AUTO_PROCESSES, ServerSettings, resolve_processes


def test_defaults_match_the_shipped_config() -> None:
    settings = ServerSettings()
    assert settings.host == "[::]"
    assert settings.port == DEFAULT_GRPC_PORT
    assert settings.max_workers == 100
    assert settings.async_mode is False


def test_concurrency_limits_are_derived_when_left_at_zero() -> None:
    settings = ServerSettings(max_workers=10)
    assert settings.concurrent_rpcs == 80
    assert settings.concurrent_streams == 100

    wide = ServerSettings(max_workers=100)
    assert wide.concurrent_rpcs == 800
    assert wide.concurrent_streams == 800


def test_explicit_concurrency_limits_win() -> None:
    settings = ServerSettings(max_workers=10, max_concurrent_rpcs=50, max_concurrent_streams=7)
    assert settings.concurrent_rpcs == 50
    assert settings.concurrent_streams == 7


def test_workers_must_be_positive() -> None:
    with pytest.raises(ConfigError, match="max_workers"):
        ServerSettings(max_workers=0)


def test_admission_limit_below_the_pool_size_is_refused() -> None:
    with pytest.raises(ConfigError, match="max_concurrent_rpcs"):
        ServerSettings(max_workers=10, max_concurrent_rpcs=5)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("gzip", grpc.Compression.Gzip),
        ("GZIP", grpc.Compression.Gzip),
        ("deflate", grpc.Compression.Deflate),
        ("none", grpc.Compression.NoCompression),
        ("off", grpc.Compression.NoCompression),
        ("identity", grpc.Compression.NoCompression),
        ("nonsense", grpc.Compression.Gzip),
    ],
)
def test_compression_names(name: str, expected: grpc.Compression) -> None:
    assert ServerSettings.from_config({"compression": name}).grpc_compression is expected


def test_listen_addr() -> None:
    assert ServerSettings(host="127.0.0.1", port=9601).listen_addr == "127.0.0.1:9601"


def test_replace_endpoint_keeps_everything_else() -> None:
    base = ServerSettings(max_workers=7, compression="deflate", async_mode=True)
    moved = base.replace_endpoint("1.2.3.4", 1234)

    assert (moved.host, moved.port) == ("1.2.3.4", 1234)
    assert (moved.max_workers, moved.compression, moved.async_mode) == (7, "deflate", True)
    assert base.replace_endpoint(None, None) is base
    assert base.replace_endpoint(port=1).host == base.host


def test_from_config_reads_every_field() -> None:
    settings = ServerSettings.from_config(
        {
            "host": " 0.0.0.0 ",
            "port": "9601",
            "max_workers": 4,
            "max_concurrent_rpcs": 40,
            "max_concurrent_streams": 200,
            "processes": 2,
            "compression": "None",
            "async_mode": "true",
        }
    )
    assert settings.host == "0.0.0.0"
    assert settings.port == 9601
    assert settings.processes == 2
    assert settings.async_mode is True
    assert settings.grpc_compression is grpc.Compression.NoCompression


def test_from_config_is_loud_about_a_bad_async_mode() -> None:
    with pytest.raises(ConfigError, match="async_mode"):
        ServerSettings.from_config({"async_mode": "sometimes"})


def test_from_config_is_loud_about_a_bad_worker_count() -> None:
    with pytest.raises(ConfigError, match="max_workers"):
        ServerSettings.from_config({"max_workers": 0})


def test_from_config_tolerates_an_empty_section() -> None:
    assert ServerSettings.from_config(None) == ServerSettings()
    assert ServerSettings.from_config({}) == ServerSettings()


def test_processes_zero_means_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_settings, "fork_supported", lambda: True)
    resolved = resolve_processes(0)
    assert 1 <= resolved <= MAX_AUTO_PROCESSES


def test_multiprocess_degrades_where_fork_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_settings, "fork_supported", lambda: False)
    assert resolve_processes(4) == 1
    assert resolve_processes(1) == 1
