from __future__ import annotations

import socket
from typing import Any

import pytest

from ipclick.cluster.node import ClusterConfig
from ipclick.multiprocess import probe_port
from ipclick.server_settings import ServerSettings
from ipclick.services.task_service import TaskService
from ipclick.trace import TraceRecorder
from ipclick.utils.config_util import Settings
from ipclick.web.snapshot import BROWSER_DISABLED, build_cluster, build_dashboard, build_live


class FakeView:
    def __init__(self, config: Settings, task_service: TaskService | None = None) -> None:
        self.config: Settings = config
        self.settings: ServerSettings = ServerSettings(host="127.0.0.1", port=9601, max_workers=8)
        self.cluster_config: ClusterConfig = ClusterConfig.from_config(config.get("CLUSTER"))
        self.recorder: TraceRecorder = TraceRecorder()
        self.task_service: TaskService | None = task_service
        self.listen_addr: str = "127.0.0.1:9601"
        self.drained: set[str] = set()
        self.extras: dict[str, Any] = {}

    @property
    def web_address(self) -> str:
        return "127.0.0.1:9527"

    @property
    def web_port(self) -> int:
        return 9527

    def dashboard_extras(self) -> dict[str, Any]:
        return self.extras

    def observed_nodes(self) -> list[dict[str, Any]]:
        return [{"id": "n1", "address": "127.0.0.1:9601"}]


@pytest.fixture
def view() -> FakeView:
    return FakeView(
        Settings(
            {
                "SECURITY": {"block_private_networks": True},
                "DOWNLOADER": {"concurrency": {"per_host_max_concurrent": 4}},
                "BROWSER": {"enabled": False},
                "CLUSTER": {},
                "CLIENT": {},
            }
        )
    )


def test_dashboard_reports_the_server_identity(view: FakeView) -> None:
    server = build_dashboard(view)["server"]

    assert server["grpc_address"] == "127.0.0.1:9601"
    assert server["grpc_port"] == 9601
    assert server["max_workers"] == 8
    assert server["async_mode"] is False
    assert server["web_port"] == 9527
    assert "curl_cffi" in server["adapters"]
    assert server["mode"] == "standalone"


def test_dashboard_surfaces_the_security_posture(view: FakeView) -> None:
    security = build_dashboard(view)["security"]

    assert security["block_private_networks"] is True
    assert security["block_metadata_endpoints"] is True
    assert security["auth"] is False
    assert "未启用" in security["tls"]


def test_dashboard_surfaces_the_limits(view: FakeView) -> None:
    assert build_dashboard(view)["limits"]["per_host_max_concurrent"] == 4


def test_dashboard_says_so_when_the_browser_is_off(view: FakeView) -> None:
    assert build_dashboard(view)["browser"]["engine"] == BROWSER_DISABLED


def test_dashboard_prefers_the_web_page_extras(view: FakeView) -> None:
    view.extras = {"config_path": "/etc/ipclick.toml", "components": [{"name": "niquests"}]}
    server = build_dashboard(view)

    assert server["server"]["config_path"] == "/etc/ipclick.toml"
    assert server["components"] == [{"name": "niquests"}]


def test_a_broken_mode_is_reported_instead_of_raised() -> None:
    broken = FakeView(Settings({"GENERAL": {"mode": "cluster"}, "CLUSTER": {}, "BROWSER": {"enabled": False}}))
    assert "配置错误" in build_dashboard(broken)["server"]["mode"]


def test_cluster_summary_without_forwarding(view: FakeView) -> None:
    cluster = build_cluster(view)

    assert cluster["forward"] is False
    assert cluster["strategy"] == "round_robin"
    assert cluster["nodes"] == [{"id": "n1", "address": "127.0.0.1:9601"}]


def test_live_snapshot_is_just_the_counters(view: FakeView) -> None:
    live = build_live(view)
    assert set(live) == {"trace"}
    assert "process" in live["trace"]


def test_probe_port_accepts_a_free_port() -> None:
    probe_port("127.0.0.1", _free_port())


def test_probe_port_refuses_a_taken_port() -> None:
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        taken = holder.getsockname()[1]
        with pytest.raises(RuntimeError, match="无法绑定"):
            probe_port("127.0.0.1", taken)
    finally:
        holder.close()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
