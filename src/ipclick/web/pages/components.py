"""本机及远程节点的可选组件管理页面。"""

from __future__ import annotations

import json as json_lib
from typing import Any, final

from ipclick.dto.proto import task_pb2
from ipclick.exceptions import ValidationError
from ipclick.utils.config_util import section
from ipclick.utils.log_util import log
from ipclick.web.pages.context import PageContext
from ipclick.web.templates import render_components


REMOTE_COMPONENT_TIMEOUT = 15.0


def _job_from_pb(job: Any) -> dict[str, Any]:
    percent = job.percent
    return {
        "id": job.id,
        "title": job.title,
        "command": job.command,
        "status": job.status,
        "returncode": job.returncode,
        "elapsed": job.elapsed_seconds,
        "progress": {
            "percent": None if percent < 0 else round(percent, 1),
            "done_bytes": job.done_bytes,
            "speed": round(job.speed_bytes),
            "phase": job.phase,
        },
        "output": list(job.output),
    }


def _readable_component_error(error: Any, node_id: str) -> str:
    import grpc

    code = getattr(error, "code", lambda: None)()
    details = (getattr(error, "details", lambda: "")() or "").strip()

    if code is grpc.StatusCode.PERMISSION_DENIED:
        return details or f"节点 {node_id} 未开启远程组件管理"
    if code is grpc.StatusCode.UNIMPLEMENTED:
        return f"节点 {node_id} 没有远程组件管理接口（版本太旧）——先把那台升级上来"
    if code is grpc.StatusCode.UNAUTHENTICATED:
        return f"节点 {node_id} 鉴权不通过：两端的 IPCLICK_CLUSTER_SECRET 必须完全一致。{details}"
    if code is grpc.StatusCode.UNAVAILABLE:
        return f"连不上节点 {node_id}：{details}"
    name = getattr(code, "name", str(code))
    return f"节点 {node_id} 返回 {name}{f'：{details}' if details else ''}"


@final
class ComponentsPage:
    """呈现组件状态，并将受限安装动作路由到本机或集群节点。"""

    def __init__(self, ctx: PageContext) -> None:
        self.ctx: PageContext = ctx

    def components_page(self, username: str, csrf: str, *, node_id: str = "") -> str:
        """渲染本机或指定远程节点的组件页面。"""
        from ipclick.adapters.browser_engines import playwright_registry_dir
        from ipclick.adapters.browser_settings import BrowserSettings
        from ipclick.components import COMPONENTS, snapshot
        from ipclick.web.installer import browser_body_location, detect_toolchain

        messages, errors = self.ctx.take_flash()
        nodes = self.ctx.target_nodes()

        if node_id:
            remote = self.remote_component(node_id, "list")
            if not remote["ok"]:
                errors = [*errors, remote["message"]]
            return render_components(
                remote.get("components") or [],
                username,
                csrf,
                toolchain=f"节点 {node_id} 自己的环境",
                job=remote.get("job"),
                messages=messages,
                errors=errors,
                bodies={},
                registry_dir="（在那台机器上）",
                nodes=nodes,
                active_node=node_id,
                remote=True,
            )

        toolchain = detect_toolchain()
        bodies = {c.extra: browser_body_location(c) for c in COMPONENTS if c.kind == "browser"}
        registry = playwright_registry_dir("playwright")
        return render_components(
            snapshot(BrowserSettings.from_config(section(self.ctx.config, "BROWSER"))),
            username,
            csrf,
            toolchain=toolchain.describe() if toolchain else "既没有 pip 也没有 uv —— 无法从页面安装",
            job=self.ctx.installer.current(),
            messages=messages,
            errors=errors,
            bodies=bodies,
            registry_dir=str(registry) if registry else "~/.cache/ms-playwright",
            nodes=nodes,
            active_node="",
            remote=False,
        )

    def remote_component(self, node_id: str, op: str, extra: str = "", browser_kind: str = "") -> dict[str, Any]:
        """通过带内部鉴权的 RPC 调用远程节点组件接口。"""
        import grpc

        try:
            response = self.ctx.call_node(
                node_id,
                lambda stub, metadata, timeout: stub.Component(
                    task_pb2.ComponentReq(
                        op=op,
                        extra=extra,
                        browser_kind=browser_kind,
                        from_node=self.ctx.self_id(),
                    ),
                    timeout=timeout,
                    metadata=metadata,
                ),
                timeout=REMOTE_COMPONENT_TIMEOUT,
            )
        except ValidationError as e:
            return {"ok": False, "message": str(e), "node_id": node_id}
        except grpc.RpcError as e:
            return {"ok": False, "message": _readable_component_error(e, node_id), "node_id": node_id}
        except Exception as e:
            log.exception(f"远程组件操作失败（{node_id}）：{e}")
            return {"ok": False, "message": f"{type(e).__name__}: {e}", "node_id": node_id}

        components: list[dict[str, Any]] = []
        if response.components_json:
            try:
                components = json_lib.loads(response.components_json)
            except ValueError as e:
                log.warning(f"节点 {node_id} 返回的组件状态不是合法 JSON：{e}")

        return {
            "ok": response.ok,
            "message": response.message,
            "node_id": response.node_id or node_id,
            "components": components,
            "job": _job_from_pb(response.job) if response.HasField("job") else None,
        }

    def component_action(self, op: str, extra: str, node_id: str = "") -> tuple[bool, str]:
        """执行经过 installer/RPC 白名单校验的组件动作。"""
        from ipclick.adapters.browser_settings import BrowserSettings

        kind = BrowserSettings.from_config(section(self.ctx.config, "BROWSER")).kind
        if node_id:
            remote_op = "browser" if op == "fetch" else op
            result = self.remote_component(node_id, remote_op, extra, kind)
            return bool(result["ok"]), str(result["message"])

        if op == "install":
            return self.ctx.installer.install(extra)
        if op == "uninstall":
            return self.ctx.installer.uninstall(extra)
        if op == "fetch":
            return self.ctx.installer.fetch_browser(extra, kind)
        return False, f"未知操作 {op!r}"

    def component_status(self, node_id: str = "") -> dict[str, Any] | None:
        """返回本机或远程节点当前组件任务状态。"""
        if not node_id:
            return self.ctx.installer.current()
        return self.remote_component(node_id, "status").get("job")

    def refresh_components(self) -> tuple[bool, str]:
        """刷新本机适配器注册表与组件探测结果。"""
        from ipclick.adapters import registry

        registry.refresh()
        return True, "已重新探测各组件的安装状态"
