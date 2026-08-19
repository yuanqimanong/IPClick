from __future__ import annotations

from typing import Any, Protocol

from ipclick import __version__
from ipclick.adapters.browser_settings import BrowserSettings, resolve_max_pages
from ipclick.auth import load_tokens
from ipclick.cluster.node import ClusterConfig
from ipclick.cluster.tokens import cluster_secret
from ipclick.compression import CompressionPolicy
from ipclick.factory import resolve_mode
from ipclick.limiter import LimiterSettings
from ipclick.server_settings import ServerSettings, resolve_processes
from ipclick.services.task_service import TaskService
from ipclick.tls import TLSSettings, describe
from ipclick.trace import TraceRecorder
from ipclick.utils.config_util import Settings, section


RECENT_ON_DASHBOARD = 12

BROWSER_DISABLED = "已关闭"


class RuntimeView(Protocol):
    config: Settings
    settings: ServerSettings
    cluster_config: ClusterConfig
    recorder: TraceRecorder
    task_service: TaskService | None
    listen_addr: str
    drained: set[str]

    @property
    def web_address(self) -> str: ...

    @property
    def web_port(self) -> int: ...

    def dashboard_extras(self) -> dict[str, Any]: ...

    def observed_nodes(self) -> list[dict[str, Any]]: ...


def _describe_engine(browser: BrowserSettings) -> str:
    from ipclick.adapters.browser_engines import resolve_engine

    if not browser.enabled:
        return BROWSER_DISABLED
    try:
        return resolve_engine(browser.engine)
    except Exception as e:
        return f"配置错误: {e}"


def _describe_mode(config: Settings) -> str:
    try:
        return resolve_mode(config)
    except Exception as e:
        return f"配置错误: {e}"


def build_live(view: RuntimeView) -> dict[str, Any]:
    return {"trace": view.recorder.stats()}


def build_cluster(view: RuntimeView) -> dict[str, Any]:
    from ipclick.cluster.forwarder import ForwardingTaskService

    service = view.task_service
    if isinstance(service, ForwardingTaskService):
        data: dict[str, Any] = service.snapshot()
        for node in list(data.get("nodes") or []):
            node["drained"] = node.get("id") in view.drained
            node["is_self"] = node.get("id") == service.self_id
        return data

    return {
        "forward": False,
        "strategy": view.cluster_config.strategy,
        "self_id": service.node_id if service is not None else "",
        "internal_auth": bool(cluster_secret(section(view.config, "CLUSTER"))),
        "nodes": view.observed_nodes(),
    }


def build_dashboard(view: RuntimeView) -> dict[str, Any]:
    from ipclick.adapters.registry import ADAPTER_CLASSES, DEFAULT_ADAPTER_NAME

    security = section(view.config, "SECURITY")
    limits = LimiterSettings.from_config(section(view.config, "DOWNLOADER"))
    browser = BrowserSettings.from_config(section(view.config, "BROWSER"))
    engine = _describe_engine(browser)
    extras = view.dashboard_extras()
    settings = view.settings

    return {
        "server": {
            "address": view.listen_addr,
            "grpc_address": settings.listen_addr,
            "grpc_port": settings.port,
            "web_address": view.web_address,
            "web_port": view.web_port,
            "version": __version__,
            "mode": _describe_mode(view.config),
            "node_id": view.task_service.node_id if view.task_service is not None else view.recorder.node_id,
            "max_workers": settings.max_workers,
            "processes": resolve_processes(settings.processes),
            "async_mode": settings.async_mode,
            "default_adapter": DEFAULT_ADAPTER_NAME,
            "adapters": sorted(ADAPTER_CLASSES),
            "compression": CompressionPolicy(section(view.config, "CLIENT")).describe(),
            "config_path": extras.get("config_path", "—"),
        },
        "trace": extras.get("trace") or view.recorder.stats(),
        "recent": extras.get("recent") or view.recorder.recent(limit=RECENT_ON_DASHBOARD),
        "components": extras.get("components") or [],
        "security": {
            "tls": describe(TLSSettings.from_config(security)),
            "auth": bool(load_tokens(security)),
            "block_private_networks": security.get("block_private_networks", False),
            "block_metadata_endpoints": security.get("block_metadata_endpoints", True),
        },
        "limits": {
            "per_host_max_concurrent": limits.per_host_max_concurrent,
            "per_host_qps": limits.per_host_qps,
            "wait_timeout": limits.wait_timeout,
        },
        "browser": {
            "engine": engine,
            "max_pages": browser.max_pages,
            "max_pages_effective": resolve_max_pages(browser.max_pages, engine),
            "allow_scripts": browser.allow_scripts,
        },
        "cluster": build_cluster(view),
    }


__all__ = ["BROWSER_DISABLED", "RECENT_ON_DASHBOARD", "RuntimeView", "build_cluster", "build_dashboard", "build_live"]
