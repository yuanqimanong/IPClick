from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from ipclick.exceptions import ConfigError
from ipclick.factory import (
    _LazyDownloader,
    close_all_downloaders,
    create_client,
    downloader,
    get_downloader,
    resolve_mode,
)
from ipclick.protocols import DownloadClient
from ipclick.sdk import Downloader
from ipclick.utils.config_util import Settings


def test_standalone_is_the_default() -> None:
    assert resolve_mode(Settings({})) == "standalone"
    assert resolve_mode(None) == "standalone"


def test_unknown_mode_is_refused() -> None:
    with pytest.raises(ConfigError, match="GENERAL"):
        resolve_mode(Settings({"GENERAL": {"mode": "swarm"}}))


def test_cluster_mode_without_nodes_is_refused() -> None:
    with pytest.raises(ConfigError, match="cluster"):
        resolve_mode(Settings({"GENERAL": {"mode": "cluster"}}))


def test_cluster_mode_accepts_discovery_instead_of_nodes() -> None:
    config = Settings({"GENERAL": {"mode": "cluster"}, "CLUSTER": {"discovery": {"mode": "dns"}}})
    assert resolve_mode(config) == "cluster"


def test_auto_mode_follows_the_node_list() -> None:
    nodes = Settings({"GENERAL": {"mode": "auto"}, "CLUSTER": {"nodes": [{"address": "127.0.0.1:9601"}]}})
    assert resolve_mode(nodes) == "cluster"
    assert resolve_mode(Settings({"GENERAL": {"mode": "auto"}})) == "standalone"


def test_create_client_returns_a_standalone_downloader() -> None:
    client = create_client()
    try:
        assert isinstance(client, Downloader)
        assert isinstance(client, DownloadClient)
    finally:
        client.close()


def test_get_downloader_caches_per_target() -> None:
    try:
        first = get_downloader()
        assert get_downloader() is first
        other = get_downloader(host="127.0.0.1", port=19999)
        assert other is not first
        assert get_downloader(host="127.0.0.1", port=19999) is other
    finally:
        close_all_downloaders()


def test_closing_clears_the_cache() -> None:
    first = get_downloader()
    close_all_downloaders()
    assert get_downloader() is not first
    close_all_downloaders()


def test_the_lazy_proxy_satisfies_the_client_protocol() -> None:
    assert isinstance(downloader, DownloadClient)
    assert "lazy" in repr(downloader)


def test_the_lazy_proxy_reuses_the_cached_client() -> None:
    try:
        assert cast(_LazyDownloader, downloader).client is get_downloader()
    finally:
        close_all_downloaders()


def test_cluster_mode_ignores_host_and_port(tmp_path: Path) -> None:
    (tmp_path / "ipclick.toml").write_text(
        '[GENERAL]\nmode = "cluster"\n[CLUSTER]\nnodes = [{ address = "127.0.0.1:19998" }]\n',
        encoding="utf-8",
    )
    client = create_client(host="10.0.0.1", port=1234)
    try:
        assert not isinstance(client, Downloader)
    finally:
        client.close()
