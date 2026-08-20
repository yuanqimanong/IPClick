"""各 Web 页面共享的配置、运行时服务与短期消息上下文。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import threading
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
    """聚合页面依赖，并协调配置重载、集群热更新和后台安装。"""

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
        self._flash_lock: threading.Lock = threading.Lock()

        self.installer: InstallManager = InstallManager()
        self.installer.on_finished = self.after_install

    def fail(self, *errors: str) -> None:
        """替换下一次页面渲染展示的错误消息。"""
        with self._flash_lock:
            self._errors = list(errors)

    def notify(self, *messages: str) -> None:
        """替换下一次页面渲染展示的成功消息。"""
        with self._flash_lock:
            self._messages = list(messages)

    def add_error(self, error: str) -> None:
        """追加一条待展示错误。"""
        with self._flash_lock:
            self._errors.append(error)

    def add_message(self, message: str) -> None:
        """追加一条待展示消息。"""
        with self._flash_lock:
            self._messages.append(message)

    def extend_first_message(self, suffix: str) -> None:
        """为首条成功消息补充上下文。"""
        with self._flash_lock:
            if self._messages:
                self._messages[0] += suffix

    def config_text(self) -> str:
        """读取目标配置；文件尚不存在时返回内置模板。"""
        if self.config_path.exists():
            return self.config_path.read_text(encoding="utf-8")
        from ipclick.config_loader.loader import example_config

        log.info(f"{self.config_path} 不存在，将以默认模板为基础创建")
        return example_config()

    def reload_config(self) -> None:
        """清除加载缓存并刷新页面持有的配置快照。"""
        from ipclick.config_loader.loader import load_config

        try:
            load_config.cache_clear()
            self.config = load_config(str(self.config_path) if self.config_path.exists() else None)
        except Exception as e:
            log.warning(f"重新加载配置失败：{e}")

    def self_id(self) -> str:
        """返回当前任务服务声明的节点 id。"""
        service = self.task_service
        return str(getattr(service, "self_id", "") or getattr(service, "node_id", "") or "")

    def nodes(self) -> list[dict[str, Any]]:
        """返回附带鉴权来源和本机标记的配置节点列表。"""
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
        """返回集群领域模型中的现有节点。"""
        from ipclick.cluster.node import ClusterConfig

        return ClusterConfig.from_config(section(self.config, "CLUSTER")).nodes

    def target_nodes(self) -> list[dict[str, Any]]:
        """返回测试页可选择的运行时或配置节点。"""
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
        """仅向配置内节点建立带 TLS 和集群 token 的短生命周期 RPC。"""
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
        """原子语义地取走待展示消息与错误。"""
        with self._flash_lock:
            messages, errors = self._messages, self._errors
            self._messages, self._errors = [], []
            return messages, errors

    def hot_reload_cluster(self) -> None:
        """请求运行时重载已写盘的集群配置，并保留失败提示。"""
        if self._on_cluster_changed is None:
            return
        from ipclick.server_settings import ServerSettings, resolve_processes

        configured_processes = ServerSettings.from_config(section(self.config, "SERVER")).processes
        if resolve_processes(configured_processes) > 1:
            # Web 只运行在 worker 0；在没有进程间广播前，只更新它会造成
            # 路由和鉴权状态分裂。文件已写盘，统一重启后由所有 worker 读取。
            self.add_message("当前为多进程模式：集群配置已保存，需重启 ipclick 才会在全部 worker 生效")
            return
        try:
            ok, message = self._on_cluster_changed()
        except Exception as e:
            log.exception(f"集群配置热更新失败：{e}")
            self.add_error(f"已写回文件，但热更新失败（重启后仍会生效）：{type(e).__name__}: {e}")
        else:
            (self.add_message if ok else self.add_error)(message)

    def preserve_node_fields(self, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """编辑节点表格时保留页面未暴露的 token、region 与 zone。"""
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
        """从推荐端口区间选择第一个未被节点配置占用的端口。"""
        used = {int(port) for node in self.nodes() if (port := node["address"].rpartition(":")[2]).isdigit()}
        for candidate in range(NODE_PORT_BASE, NODE_PORT_BASE + 10000):
            if candidate not in used:
                return candidate
        return NODE_PORT_BASE

    def after_install(self, job: Any) -> None:
        """后台依赖任务结束后刷新适配器注册表。"""
        from ipclick.adapters import registry

        registry.refresh()
        log.debug(f"依赖任务 {getattr(job, 'title', '')} 结束，已刷新组件状态与适配器注册表")


__all__ = ["NODE_PORT_BASE", "PageContext"]
