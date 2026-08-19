from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, final

from ipclick.exceptions import ValidationError
from ipclick.services.task_service import TaskService
from ipclick.trace import TraceRecorder
from ipclick.utils.config_util import Settings, section
from ipclick.utils.log_util import log
from ipclick.web.installer import InstallManager


NODE_PORT_BASE = 19001


@final
class PageContext:
    def __init__(
        self,
        config: Settings,
        recorder: TraceRecorder,
        *,
        task_service: TaskService | None = None,
        config_path: str | Path | None = None,
        cluster_snapshot: Any = None,
        on_cluster_changed: Callable[[], tuple[bool, str]] | None = None,
        cli_port: int | None = None,
        runtime_ports: dict[str, int] | None = None,
    ) -> None:
        from ipclick.config_loader.writer import target_path

        self.config: Settings = config
        self.runtime_ports: dict[str, int] = dict(runtime_ports or {})
        self.recorder: TraceRecorder = recorder
        self.task_service: TaskService | None = task_service
        self.config_path: Path = target_path(config_path, cli_port)

        self._cluster_snapshot: Any = cluster_snapshot
        self._on_cluster_changed: Callable[[], tuple[bool, str]] | None = on_cluster_changed
        self._messages: list[str] = []
        self._errors: list[str] = []

        self.installer: InstallManager = InstallManager()
        self.installer.on_finished = self.after_install

    def fail(self, *errors: str) -> None:
        self._errors = list(errors)

    def notify(self, *messages: str) -> None:
        self._messages = list(messages)

    def add_error(self, error: str) -> None:
        self._errors.append(error)

    def add_message(self, message: str) -> None:
        self._messages.append(message)

    def extend_first_message(self, suffix: str) -> None:
        if self._messages:
            self._messages[0] += suffix

    def config_text(self) -> str:
        if self.config_path.exists():
            return self.config_path.read_text(encoding="utf-8")
        from ipclick.config_loader.loader import example_config

        log.info(f"{self.config_path} 不存在，将以默认模板为基础创建")
        return example_config()

    def reload_config(self) -> None:
        from ipclick.config_loader.loader import load_config

        try:
            load_config.cache_clear()
            self.config = load_config(str(self.config_path) if self.config_path.exists() else None)
        except Exception as e:
            log.warning(f"重新加载配置失败：{e}")

    def self_id(self) -> str:
        service = self.task_service
        return str(getattr(service, "self_id", "") or getattr(service, "node_id", "") or "")

    def nodes(self) -> list[dict[str, Any]]:
        from ipclick.cluster.node import ClusterConfig
        from ipclick.cluster.tokens import cluster_secret

        cluster_section = section(self.config, "CLUSTER")
        cluster = ClusterConfig.from_config(cluster_section)
        secret = cluster_secret(cluster_section)
        self_id = self.self_id()
        return [
            {
                "id": node.id,
                "address": node.address,
                "weight": node.weight,
                "index": index,
                "is_self": node.id == self_id,
                "token_source": "节点列表内指定" if node.token else ("由共享密钥派生" if secret else "无（不鉴权）"),
            }
            for index, node in enumerate(cluster.nodes)
        ]

    def existing_nodes(self) -> tuple[Any, ...]:
        from ipclick.cluster.node import ClusterConfig

        return ClusterConfig.from_config(section(self.config, "CLUSTER")).nodes

    def target_nodes(self) -> list[dict[str, Any]]:
        service = self.task_service
        cluster = getattr(service, "cluster", None) if service is not None else None
        nodes = list(getattr(cluster, "nodes", ()) or [])
        if not nodes:
            try:
                from ipclick.cluster.node import ClusterConfig

                nodes = list(ClusterConfig.from_config(section(self.config, "CLUSTER")).nodes)
            except Exception as e:
                log.debug(f"读集群节点列表失败，「试一试」不显示目标节点：{e}")
                return []

        self_id = self.self_id()
        forwarding = callable(getattr(service, "send_to_node", None))
        return [
            {"id": node.id, "address": node.address, "is_self": node.id == self_id, "forwarding": forwarding}
            for node in nodes
        ]

    def call_node(self, node_id: str, call: Callable[[Any, Any, float], Any], *, timeout: float) -> Any:

        from ipclick.auth import build_client_metadata
        from ipclick.cluster.node import ClusterConfig
        from ipclick.cluster.tokens import cluster_secret, token_for
        from ipclick.dto.proto import task_pb2_grpc
        from ipclick.rpc import open_channel_for
        from ipclick.tls import TLSSettings

        cluster_section = section(self.config, "CLUSTER")
        parsed = ClusterConfig.from_config(cluster_section)
        node = next((n for n in parsed.nodes if n.id == node_id), None)
        if node is None:
            raise ValidationError(f"节点 {node_id!r} 不在集群节点列表里，已有：{[n.id for n in parsed.nodes]}")

        tls = TLSSettings.from_config(section(self.config, "SECURITY"))
        token = token_for(node.id, node.token, cluster_secret(cluster_section))
        channel = open_channel_for(node.address, tls)
        try:
            return call(task_pb2_grpc.TaskServiceStub(channel), build_client_metadata(token), timeout)
        finally:
            channel.close()

    def take_flash(self) -> tuple[list[str], list[str]]:
        messages, errors = self._messages, self._errors
        self._messages, self._errors = [], []
        return messages, errors

    def hot_reload_cluster(self) -> None:
        if self._on_cluster_changed is None:
            return
        try:
            ok, message = self._on_cluster_changed()
        except Exception as e:
            log.exception(f"集群配置热更新失败：{e}")
            self.add_error(f"已写回文件，但热更新失败（重启后仍会生效）：{type(e).__name__}: {e}")
        else:
            (self.add_message if ok else self.add_error)(message)

    def preserve_node_fields(self, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        preserved = {n.id: n for n in self.existing_nodes()}
        for node in nodes:
            old = preserved.get(str(node["id"]))
            if old is None:
                continue
            for key in ("token", "region", "zone"):
                if value := getattr(old, key, ""):
                    node[key] = value
        return nodes

    def next_node_port(self) -> int:
        used = {int(port) for node in self.nodes() if (port := node["address"].rpartition(":")[2]).isdigit()}
        for candidate in range(NODE_PORT_BASE, NODE_PORT_BASE + 10000):
            if candidate not in used:
                return candidate
        return NODE_PORT_BASE

    def after_install(self, job: Any) -> None:
        from ipclick.adapters import registry

        registry.refresh()
        log.debug(f"依赖任务 {getattr(job, 'title', '')} 结束，已刷新组件状态与适配器注册表")


__all__ = ["NODE_PORT_BASE", "PageContext"]
