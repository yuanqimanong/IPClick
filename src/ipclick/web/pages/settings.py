"""配置编辑、集群节点管理、凭据生成与部署计划页面。"""

from __future__ import annotations

from collections import OrderedDict
import secrets
import threading
from typing import TYPE_CHECKING, Any, final

from ipclick.exceptions import ConfigError
from ipclick.server_settings import ServerSettings
from ipclick.utils.config_util import section
from ipclick.utils.log_util import log
from ipclick.web.editable import current_value, groups_for, parse_form, parse_nodes, validate_nodes
from ipclick.web.pages.context import PageContext
from ipclick.web.templates import render_config


if TYPE_CHECKING:
    from ipclick.web.deploy import NodePlan


SECRET_KEEP = 8


def _same_value(current: Any, new: Any) -> bool:
    if isinstance(current, bool) or isinstance(new, bool):
        return isinstance(current, bool) and isinstance(new, bool) and current == new
    if isinstance(current, (int, float)) and isinstance(new, (int, float)):
        return float(current) == float(new)
    return current == new


def _probe_title(result: Any) -> str:
    if not result.reachable:
        return "连不上"
    if result.authenticated is False:
        return "鉴权不通过"
    if result.remote_auth_required is False:
        return "通过（对方未设防）"
    return "通过"


@final
class SettingsPage:
    """协调配置文件写回、有限热更新及集群配置操作。"""

    def __init__(self, ctx: PageContext) -> None:
        self.ctx: PageContext = ctx
        self._generated: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._secret_lock: threading.Lock = threading.Lock()
        # ThreadingHTTPServer 会并行处理多个表单。读旧配置、合并并落盘必须
        # 作为一个事务串行执行，否则两个标签页可能互相覆盖刚保存的字段。
        self._config_lock: threading.RLock = threading.RLock()

    def _groups(self, tab: str = "basic") -> list[tuple[str, list[dict[str, Any]]]]:
        groups: list[tuple[str, list[dict[str, Any]]]] = []
        for title, fields in groups_for(tab):
            items = [
                {
                    "name": field.name,
                    "label": field.label,
                    "kind": field.kind,
                    "value": current_value(self.ctx.config, field),
                    "choices": field.choices,
                    "hint": field.hint,
                    "restart": field.restart,
                    "running": self._running_mismatch(field.name, current_value(self.ctx.config, field)),
                }
                for field in fields
            ]
            groups.append((title, items))
        return groups

    def _running_mismatch(self, name: str, file_value: Any) -> int:
        actual = self.ctx.runtime_ports.get(name)
        if not actual:
            return 0
        try:
            same = int(file_value) == actual
        except (TypeError, ValueError):
            same = False
        return 0 if same else actual

    def _readonly(self) -> list[tuple[str, Any]]:
        from ipclick.auth import load_tokens
        from ipclick.secrets import SECRETS, describe_source
        from ipclick.tls import TLSSettings, describe
        from ipclick.web.templates import esc

        security = section(self.ctx.config, "SECURITY")
        rows: list[tuple[str, Any]] = [
            ("传输层 [SECURITY.tls]", esc(describe(TLSSettings.from_config(security)))),
            ("令牌鉴权 [SECURITY].auth_token", "已配置" if load_tokens(security) else "未配置"),
            ("拦截内网地址", esc(security.get("block_private_networks", False))),
            ("拦截元数据端点", esc(security.get("block_metadata_endpoints", True))),
            ("允许的协议", esc(", ".join(security.get("allowed_schemes", ["http", "https"])))),
            (
                "允许页内 JS [BROWSER].allow_scripts",
                esc(section(self.ctx.config, "BROWSER").get("allow_scripts", False)),
            ),
        ]
        rows.extend((f"机密 {spec.label}", esc(describe_source(self.ctx.config, spec))) for spec in SECRETS)
        return rows

    def _generators(self) -> list[dict[str, Any]]:
        from ipclick.secrets import SECRETS, describe_source

        return [
            {
                "env": spec.env,
                "label": spec.label,
                "shared": spec.shared,
                "note": spec.note,
                "source": describe_source(self.ctx.config, spec),
            }
            for spec in SECRETS
            if spec.generatable
        ]

    def config_page(self, username: str, csrf: str, *, generated_token: str = "", tab: str = "basic") -> str:
        """渲染基础配置或集群配置页。"""
        messages, errors = self.ctx.take_flash()
        return render_config(
            self._groups(tab),
            username,
            csrf,
            config_path=str(self.ctx.config_path),
            messages=messages,
            errors=errors,
            readonly_note=self._readonly(),
            generators=self._generators(),
            generated=self.take_generated(generated_token),
            tab=tab,
            cluster=self._cluster_tab_data() if tab == "cluster" else None,
        )

    def _cluster_tab_data(self) -> dict[str, Any]:
        from ipclick.auth import load_tokens
        from ipclick.cluster.tokens import cluster_secret

        cluster_section = section(self.ctx.config, "CLUSTER")
        return {
            "forward": str(cluster_section.get("forward", "off")).strip().lower() == "on",
            "nodes": self.ctx.nodes(),
            "auth_configured": bool(load_tokens(section(self.ctx.config, "SECURITY"))),
            "secret_configured": bool(cluster_secret(cluster_section)),
            "next_port": self.ctx.next_node_port(),
        }

    def _deploy_plans(self) -> list[NodePlan]:
        from ipclick.auth import load_tokens
        from ipclick.cluster.tokens import cluster_secret
        from ipclick.web.deploy import build_plan

        cluster_section = section(self.ctx.config, "CLUSTER")
        tokens = load_tokens(section(self.ctx.config, "SECURITY"))
        nodes = [{"id": n["id"], "address": n["address"]} for n in self.ctx.nodes() if n.get("address")]
        forward = str(cluster_section.get("forward", "off")).strip().lower() == "on"
        max_workers = ServerSettings.from_config(section(self.ctx.config, "SERVER")).max_workers
        return [
            build_plan(
                node,
                nodes=nodes,
                forward=forward,
                auth_token=tokens[0] if tokens else "（主控上还没配，先去生成一个）",
                cluster_secret=cluster_secret(cluster_section) or "（主控上还没配，先去生成一个）",
                max_workers=max_workers,
            )
            for node in nodes
        ]

    def deploy_plan(self, node_id: str) -> NodePlan | None:
        """返回指定节点的部署计划。"""
        return next((plan for plan in self._deploy_plans() if plan.node_id == node_id), None)

    def deploy_page(self, node_id: str, username: str, csrf: str) -> str | None:
        """渲染指定节点的部署说明；节点不存在时返回 ``None``。"""
        from ipclick.web.templates import render_deploy

        plans = self._deploy_plans()
        plan = next((p for p in plans if p.node_id == node_id), None)
        if plan is None:
            return None
        return render_deploy(plan.snapshot(), username, csrf, total_nodes=len(plans))

    def deploy_bundle(self) -> bytes:
        """将当前全部节点的部署计划打包为 ZIP。"""
        from ipclick.web.deploy import bundle

        return bundle(self._deploy_plans())

    def generate_secret(self, env: str) -> str:
        """为白名单环境变量生成只可取一次的随机值。"""
        from ipclick.secrets import SECRETS
        from ipclick.web.auth import generate_password

        spec = next((s for s in SECRETS if s.env == env and s.generatable), None)
        if spec is None:
            self.ctx.fail(f"{env!r} 不是可生成的凭据")
            return ""

        token = secrets.token_urlsafe(9)
        with self._secret_lock:
            self._generated[token] = {
                "env": spec.env,
                "label": spec.label,
                "shared": spec.shared,
                "note": spec.note,
                "value": generate_password(32),
            }
            while len(self._generated) > SECRET_KEEP:
                _ = self._generated.popitem(last=False)
        log.info(f"Web 端生成了新的 {spec.label}（{spec.env}），值只在页面上显示一次")
        return token

    def take_generated(self, token: str) -> dict[str, Any] | None:
        """按 token 原子取走生成的凭据，防止页面刷新后再次泄露。"""
        if not token:
            return None
        with self._secret_lock:
            return self._generated.pop(token, None)

    def save_config(self, form: dict[str, str], username: str, csrf: str) -> str:
        """校验表单并以保留未暴露字段的方式更新配置文件。"""
        with self._config_lock:
            return self._save_config_locked(form, username, csrf)

    def _save_config_locked(self, form: dict[str, str], username: str, csrf: str) -> str:
        """在配置事务锁内完成一次读、改、写和运行时刷新。"""
        from ipclick.config_loader.writer import save, set_nodes, set_values

        tab = form.get("tab", "basic")
        updates, restart_needed, errors = parse_form(form)
        updates, restart_needed = self._changed_only(updates, restart_needed)

        if "__present__CLUSTER.forward_on" in form:
            updates.setdefault("CLUSTER", {})["forward"] = "on" if "CLUSTER.forward_on" in form else "off"
            restart_needed.append("服务端转发")

        has_node_grid = tab == "cluster" and any(k.startswith("node_address_") for k in form)
        nodes = parse_nodes(form) if has_node_grid else []
        if has_node_grid:
            errors.extend(validate_nodes(nodes))

        if errors:
            self.ctx.fail(*errors)
            return self.config_page(username, csrf, tab=tab)
        if not updates and not has_node_grid:
            self.ctx.fail("没有可保存的改动")
            return self.config_page(username, csrf, tab=tab)

        try:
            text = self.ctx.config_text()
            new_text, changes = set_values(text, updates) if updates else (text, [])
            if has_node_grid:
                new_text = set_nodes(new_text, self.ctx.preserve_node_fields(nodes))
            # writer 负责原子落盘；验证失败前不触碰现有配置。
            _ = save(self.ctx.config_path, new_text)
        except (ConfigError, OSError) as e:
            self.ctx.fail(str(e))
            return self.config_page(username, csrf, tab=tab)

        self.ctx.notify(f"已写回 {self.ctx.config_path}（{len(changes)} 项）")
        if has_node_grid:
            self.ctx.extend_first_message(f"，{len(nodes)} 个节点")
        if restart_needed:
            self.ctx.add_message("这些项要重启 ipclick 才生效：" + "、".join(sorted(set(restart_needed))))
        self._apply_live(updates)
        log.info(f"Web 端保存配置：{'; '.join(changes) or '（仅节点）'}")
        self.ctx.reload_config()
        if tab == "cluster":
            self.ctx.hot_reload_cluster()
        return self.config_page(username, csrf, tab=tab)

    def add_node(self, form: dict[str, str], username: str, csrf: str) -> str:
        """校验地址后向本机节点列表追加条目并触发热更新。"""
        with self._config_lock:
            return self._add_node_locked(form, username, csrf)

    def _add_node_locked(self, form: dict[str, str], username: str, csrf: str) -> str:
        """在配置事务锁内追加节点，避免与其他配置写入丢更新。"""
        from ipclick.config_loader.writer import save, set_nodes

        host = (form.get("new_node_host") or "").strip().strip("[]")
        if not host:
            self.ctx.fail("请填 IP 或主机名")
            return self.config_page(username, csrf, tab="cluster")
        try:
            port = int((form.get("new_node_port") or "").strip() or self.ctx.next_node_port())
        except ValueError:
            self.ctx.fail(f"端口必须是数字，收到 {form.get('new_node_port')!r}")
            return self.config_page(username, csrf, tab="cluster")

        address = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        node_id = (form.get("new_node_id") or "").strip() or address
        try:
            weight = max(1, int((form.get("new_node_weight") or "100").strip() or 100))
        except ValueError:
            weight = 100

        existing = self.ctx.nodes()
        if any(n["id"] == node_id for n in existing):
            self.ctx.fail(f"已经有一个 id 为 {node_id!r} 的节点了")
            return self.config_page(username, csrf, tab="cluster")

        nodes = [{"id": n["id"], "address": n["address"], "weight": n["weight"]} for n in existing]
        nodes.append({"id": node_id, "address": address, "weight": weight})
        if errors := validate_nodes(nodes):
            self.ctx.fail(*errors)
            return self.config_page(username, csrf, tab="cluster")

        try:
            _ = save(self.ctx.config_path, set_nodes(self.ctx.config_text(), self.ctx.preserve_node_fields(nodes)))
        except (ConfigError, OSError) as e:
            self.ctx.fail(str(e))
            return self.config_page(username, csrf, tab="cluster")

        self.ctx.notify(f"已添加节点 {node_id}（{address}）")
        log.info(f"Web 端添加节点：{node_id} -> {address}")
        self.ctx.reload_config()
        self.ctx.hot_reload_cluster()
        return self.config_page(username, csrf, tab="cluster")

    def _changed_only(
        self, updates: dict[str, dict[str, Any]], restart_needed: list[str]
    ) -> tuple[dict[str, dict[str, Any]], list[str]]:
        from ipclick.web.editable import FIELDS

        changed: dict[str, dict[str, Any]] = {}
        labels: list[str] = []
        for name, entries in updates.items():
            for key, value in entries.items():
                field = FIELDS.get(f"{name}.{key}")
                current = current_value(self.ctx.config, field) if field is not None else None
                if field is not None and _same_value(current, value):
                    continue
                changed.setdefault(name, {})[key] = value
                if field is not None and field.restart:
                    labels.append(field.label)
        for label in restart_needed:
            if label == "服务端转发" and "CLUSTER" in changed and "forward" in changed["CLUSTER"]:
                labels.append(label)
        return changed, labels

    def remove_node(self, form: dict[str, str], username: str, csrf: str) -> str:
        """从本机节点列表删除指定节点并触发热更新。"""
        with self._config_lock:
            return self._remove_node_locked(form, username, csrf)

    def _remove_node_locked(self, form: dict[str, str], username: str, csrf: str) -> str:
        """在配置事务锁内删除节点，避免与其他配置写入丢更新。"""
        from ipclick.config_loader.writer import save, set_nodes

        node_id = (form.get("remove_node") or "").strip()
        remaining = [
            {"id": n["id"], "address": n["address"], "weight": n["weight"]}
            for n in self.ctx.nodes()
            if n["id"] != node_id
        ]
        if len(remaining) == len(self.ctx.nodes()):
            self.ctx.fail(f"没有 id 为 {node_id!r} 的节点")
            return self.config_page(username, csrf, tab="cluster")

        try:
            _ = save(self.ctx.config_path, set_nodes(self.ctx.config_text(), self.ctx.preserve_node_fields(remaining)))
        except (ConfigError, OSError) as e:
            self.ctx.fail(str(e))
            return self.config_page(username, csrf, tab="cluster")

        self.ctx.notify(f"已移除节点 {node_id}（只改了本机的节点列表，那台机器还在跑）")
        log.info(f"Web 端移除节点：{node_id}")
        self.ctx.reload_config()
        self.ctx.hot_reload_cluster()
        return self.config_page(username, csrf, tab="cluster")

    def _apply_live(self, updates: dict[str, dict[str, Any]]) -> None:
        log_updates = updates.get("LOG") or {}
        debug = bool((updates.get("GENERAL") or {}).get("debug", False))
        if log_updates.get("level") or "debug" in (updates.get("GENERAL") or {}):
            from ipclick.utils.log_util import LogUtil

            merged = {**section(self.ctx.config, "LOG"), **log_updates}
            LogUtil.init_from_config(merged, debug=debug)
            log.info(f"日志级别已即时切换为 {'DEBUG' if debug else merged.get('level', 'info')}")

        trace_updates = updates.get("TRACE") or {}
        if "memory_size" in trace_updates or "only_errors" in trace_updates or "record_url" in trace_updates:
            from dataclasses import replace

            self.ctx.recorder.settings = replace(
                self.ctx.recorder.settings,
                memory_size=int(trace_updates.get("memory_size", self.ctx.recorder.settings.memory_size)),
                only_errors=bool(trace_updates.get("only_errors", self.ctx.recorder.settings.only_errors)),
                record_url=bool(trace_updates.get("record_url", self.ctx.recorder.settings.record_url)),
            )

    def probe_node(self, node_id: str, address: str = "") -> dict[str, Any]:
        """探测已配置节点，或探测尚未保存的候选地址。"""
        from ipclick.cluster.node import ClusterConfig, Node
        from ipclick.cluster.probe import probe_node as run_probe
        from ipclick.cluster.tokens import cluster_secret
        from ipclick.tls import TLSSettings

        cluster_section = section(self.ctx.config, "CLUSTER")
        cluster = ClusterConfig.from_config(cluster_section)
        node = cluster.node_by_id(node_id)

        target = (address or "").strip()
        if node is None or (target and target != node.address):
            if not target:
                return {"ok": False, "warn": False, "title": "找不到节点", "detail": f"{node_id!r} 不在节点列表里"}
            try:
                node = Node.from_config({"id": node_id or target, "address": target})
            except Exception as e:
                return {"ok": False, "warn": False, "title": "地址不合法", "detail": str(e)}

        result = run_probe(
            node,
            secret=cluster_secret(cluster_section),
            tls=TLSSettings.from_config(section(self.ctx.config, "SECURITY")),
            from_node=self.ctx.self_id(),
        )
        return {
            "ok": result.ok,
            "warn": result.ok and result.remote_auth_required is False,
            "title": _probe_title(result),
            "detail": f"{result.detail}（{result.elapsed_ms} ms）",
            "elapsed_ms": result.elapsed_ms,
            "remote_version": result.remote_version,
        }
