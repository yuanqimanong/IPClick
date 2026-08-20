"""组合 Web 子页面，并保留兼容的统一页面门面。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, final

from ipclick.services.task_service import TaskService
from ipclick.trace import TraceRecorder
from ipclick.utils.config_util import Settings, section
from ipclick.web.pages.components import ComponentsPage
from ipclick.web.pages.context import PageContext
from ipclick.web.pages.sandbox import SandboxPage
from ipclick.web.pages.settings import SettingsPage
from ipclick.web.pages.skill import SkillPage
from ipclick.web.pages.trace import TracePage


if TYPE_CHECKING:
    from ipclick.web.deploy import NodePlan


RECENT_ON_DASHBOARD = 12


@final
class WebPages:
    """把 trace、测试、组件、Skill 与设置页面组合成统一路由接口。"""

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
    ):
        self.ctx: PageContext = PageContext(
            config,
            recorder,
            task_service=task_service,
            config_path=config_path,
            cluster_snapshot=cluster_snapshot,
            on_cluster_changed=on_cluster_changed,
            cli_port=cli_port,
            runtime_ports=runtime_ports,
        )
        self.trace: TracePage = TracePage(self.ctx)
        self.sandbox: SandboxPage = SandboxPage(self.ctx)
        self.components: ComponentsPage = ComponentsPage(self.ctx)
        self.skill: SkillPage = SkillPage(self.ctx)
        self.settings: SettingsPage = SettingsPage(self.ctx)

    @property
    def config(self) -> Settings:
        """返回页面上下文当前加载的配置。"""
        return self.ctx.config

    @property
    def config_path(self) -> Path:
        """返回页面读写的目标配置路径。"""
        return self.ctx.config_path

    @property
    def task_service(self) -> TaskService | None:
        """返回可供测试页调用的本地任务服务。"""
        return self.ctx.task_service

    @property
    def installer(self) -> Any:
        """返回共享的后台组件安装管理器。"""
        return self.ctx.installer

    def trace_page(self, query: dict[str, str], username: str, csrf: str) -> str:
        """渲染链路记录页面。"""
        return self.trace.trace_page(query, username, csrf)

    def trace_fragment(self, query: dict[str, str]) -> str:
        """渲染链路记录实时 fragment。"""
        return self.trace.trace_fragment(query)

    def trace_json(self, query: dict[str, str]) -> dict[str, Any]:
        """返回链路记录 JSON 视图。"""
        return self.trace.trace_json(query)

    def test_page(
        self,
        form: dict[str, str],
        result: dict[str, Any] | None,
        username: str,
        csrf: str,
        *,
        curl_notes: list[str] | None = None,
        curl_error: str = "",
    ) -> str:
        """渲染请求测试页面。"""
        return self.sandbox.test_page(form, result, username, csrf, curl_notes=curl_notes, curl_error=curl_error)

    def import_curl(self, form: dict[str, str]) -> tuple[dict[str, str], list[str], str]:
        """把 curl 输入转换为测试表单。"""
        return self.sandbox.import_curl(form)

    def stash_test_result(self, form: dict[str, str], result: dict[str, Any]) -> str:
        """暂存测试结果并返回 URL-safe 查询 token。"""
        return self.sandbox.stash_test_result(form, result)

    def take_test_result(self, token: str) -> tuple[dict[str, str], dict[str, Any] | None]:
        """按查询 token 读取暂存测试结果。"""
        return self.sandbox.take_test_result(token)

    def run_test(self, form: dict[str, str]) -> dict[str, Any]:
        """执行测试页构造的单次请求。"""
        return self.sandbox.run_test(form)

    def components_page(self, username: str, csrf: str, *, node_id: str = "") -> str:
        """渲染组件管理页面。"""
        return self.components.components_page(username, csrf, node_id=node_id)

    def remote_component(self, node_id: str, op: str, extra: str = "", browser_kind: str = "") -> dict[str, Any]:
        """调用远程节点的组件管理接口。"""
        return self.components.remote_component(node_id, op, extra, browser_kind)

    def component_action(self, op: str, extra: str, node_id: str = "") -> tuple[bool, str]:
        """执行本机或远程组件动作。"""
        return self.components.component_action(op, extra, node_id)

    def component_status(self, node_id: str = "") -> dict[str, Any] | None:
        """查询本机或远程组件任务。"""
        return self.components.component_status(node_id)

    def refresh_components(self) -> tuple[bool, str]:
        """刷新本机组件探测结果。"""
        return self.components.refresh_components()

    def skill_markdown(self) -> str:
        """返回内置 AI Skill 的 Markdown。"""
        return self.skill.skill_markdown()

    def skill_page(self, username: str, csrf: str) -> str:
        """渲染 AI Skill 页面。"""
        return self.skill.skill_page(username, csrf)

    def config_page(self, username: str, csrf: str, *, generated_token: str = "", tab: str = "basic") -> str:
        """渲染基础或集群配置页面。"""
        return self.settings.config_page(username, csrf, generated_token=generated_token, tab=tab)

    def save_config(self, form: dict[str, str], username: str, csrf: str) -> str:
        """校验并保存配置表单。"""
        return self.settings.save_config(form, username, csrf)

    def add_node(self, form: dict[str, str], username: str, csrf: str) -> str:
        """向本地集群配置添加一个节点。"""
        return self.settings.add_node(form, username, csrf)

    def remove_node(self, form: dict[str, str], username: str, csrf: str) -> str:
        """从本地集群配置移除一个节点。"""
        return self.settings.remove_node(form, username, csrf)

    def probe_node(self, node_id: str, address: str = "") -> dict[str, Any]:
        """探测配置节点或待添加地址。"""
        return self.settings.probe_node(node_id, address)

    def generate_secret(self, env: str) -> str:
        """生成一次性展示的配置凭据。"""
        return self.settings.generate_secret(env)

    def take_generated(self, token: str) -> dict[str, Any] | None:
        """按一次性 token 取走生成的凭据。"""
        return self.settings.take_generated(token)

    def deploy_plan(self, node_id: str) -> NodePlan | None:
        """返回指定节点部署计划。"""
        return self.settings.deploy_plan(node_id)

    def deploy_page(self, node_id: str, username: str, csrf: str) -> str | None:
        """渲染指定节点部署页面。"""
        return self.settings.deploy_page(node_id, username, csrf)

    def deploy_bundle(self) -> bytes:
        """返回全部节点的 ZIP 部署包。"""
        return self.settings.deploy_bundle()

    def dashboard_extras(self) -> dict[str, Any]:
        """返回仪表盘所需的页面层扩展快照。"""
        from ipclick.adapters.browser_engines import resolve_engine
        from ipclick.adapters.browser_settings import BrowserSettings
        from ipclick.components import snapshot

        browser = BrowserSettings.from_config(section(self.ctx.config, "BROWSER"))
        try:
            active = resolve_engine(browser.engine) if browser.enabled else ""
        except Exception:
            active = browser.engine
        return {
            "trace": self.ctx.recorder.stats(),
            "recent": self.ctx.recorder.recent(limit=RECENT_ON_DASHBOARD),
            "components": snapshot(browser),
            "active_engine": active,
            "config_path": str(self.ctx.config_path),
        }


__all__ = ["RECENT_ON_DASHBOARD", "PageContext", "WebPages"]
