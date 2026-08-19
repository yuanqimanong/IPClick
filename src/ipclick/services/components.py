from __future__ import annotations

from collections.abc import Callable
import json
from typing import TYPE_CHECKING, Any, final

import grpc

from ipclick.dto.proto import task_pb2
from ipclick.utils.coerce import as_bool, as_text
from ipclick.utils.config_util import Settings, section
from ipclick.utils.log_util import log


if TYPE_CHECKING:
    from ipclick.web.installer import InstallManager


DEFAULT_BROWSER_KIND = "chromium"

DISABLED_MESSAGE = "远程组件管理未开启"

DISABLED_DETAIL = (
    "本节点未开启远程组件管理。要允许主控代装，请在这台机器的配置里设置 "
    '[CLUSTER].allow_remote_install = true 并重启——它等于"能调本节点的人可以在本机跑 pip"，'
    "所以默认是关的。"
)

READ_ONLY_OPS = frozenset({"list", "status"})

MUTATING_OPS = frozenset({"install", "uninstall", "browser"})

SUPPORTED_OPS: frozenset[str] = READ_ONLY_OPS | MUTATING_OPS


@final
class ComponentService:
    def __init__(self, config: Settings, on_finished: Callable[[Any], None] | None = None) -> None:
        self.config: Settings = config
        self._on_finished: Callable[[Any], None] | None = on_finished
        self._manager: InstallManager | None = None

    @property
    def enabled(self) -> bool:
        return as_bool(section(self.config, "CLUSTER").get("allow_remote_install"))

    def handle(
        self, request: task_pb2.ComponentReq, context: grpc.ServicerContext, *, node_id: str
    ) -> task_pb2.ComponentResp:
        if not self.enabled:
            context.set_code(grpc.StatusCode.PERMISSION_DENIED)
            context.set_details(DISABLED_DETAIL)
            return task_pb2.ComponentResp(ok=False, message=DISABLED_MESSAGE, node_id=node_id)

        op = as_text(request.op)
        if request.from_node:
            log.info(f"节点 {request.from_node} 请求在本机执行组件操作：{op} {request.extra}")

        if op not in SUPPORTED_OPS:
            message = f"未知的组件操作 {op!r}"
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(f"{message}（可选：{' / '.join(sorted(SUPPORTED_OPS))}）")
            return task_pb2.ComponentResp(ok=False, message=message, node_id=node_id)

        ok, message = self._apply(op, request)
        return self._respond(ok=ok, message=message, node_id=node_id)

    def _apply(self, op: str, request: task_pb2.ComponentReq) -> tuple[bool, str]:
        if op in READ_ONLY_OPS:
            return True, ""
        manager = self.installer()
        if op == "install":
            return manager.install(request.extra)
        if op == "uninstall":
            return manager.uninstall(request.extra)
        return manager.fetch_browser(request.extra, request.browser_kind or DEFAULT_BROWSER_KIND)

    def _respond(self, *, ok: bool, message: str, node_id: str) -> task_pb2.ComponentResp:
        return task_pb2.ComponentResp(
            ok=ok,
            message=message,
            node_id=node_id,
            components_json=self.snapshot_json(),
            job=self.current_job(),
        )

    def installer(self) -> InstallManager:
        manager = self._manager
        if manager is None:
            from ipclick.web.installer import InstallManager

            manager = InstallManager()
            if self._on_finished is not None:
                manager.on_finished = self._on_finished
            self._manager = manager
        return manager

    def snapshot_json(self) -> str:
        from ipclick.adapters.browser_settings import BrowserSettings
        from ipclick.components import snapshot

        browser = BrowserSettings.from_config(section(self.config, "BROWSER"))
        return json.dumps(snapshot(browser), ensure_ascii=False, default=str)

    def current_job(self) -> task_pb2.ComponentJob | None:
        current = self.installer().current()
        if not current:
            return None
        progress = dict(current.get("progress") or {})
        percent = progress.get("percent")
        return task_pb2.ComponentJob(
            id=str(current.get("id", "")),
            title=str(current.get("title", "")),
            command=str(current.get("command", "")),
            status=str(current.get("status", "")),
            returncode=int(current.get("returncode") or 0),
            elapsed_seconds=int(current.get("elapsed") or 0),
            percent=float(percent) if percent is not None else -1.0,
            done_bytes=int(progress.get("done_bytes") or 0),
            speed_bytes=float(progress.get("speed") or 0.0),
            phase=str(progress.get("phase") or ""),
            output=list(current.get("output") or []),
        )


__all__ = [
    "DEFAULT_BROWSER_KIND",
    "DISABLED_DETAIL",
    "DISABLED_MESSAGE",
    "MUTATING_OPS",
    "READ_ONLY_OPS",
    "SUPPORTED_OPS",
    "ComponentService",
]
