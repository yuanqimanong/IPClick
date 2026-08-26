"""配置编辑、集群节点管理、凭据生成与部署计划页面。"""

from __future__ import annotations

from collections import OrderedDict
import os
from pathlib import Path
import secrets
import threading
from typing import TYPE_CHECKING, Any, final

from ipclick.exceptions import ConfigError, ValidationError
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


def _real_env_overrides(key: str, env_path: Path) -> bool:
    """判断某个机密当前是不是由**真实环境变量**提供的。

    不能只看 `os.environ`：启动时 `load_dotenv()` 已经把 `.env` 的值灌进去了，
    光看进程环境的话每一项都像"环境变量里有"。所以拿它和 `.env` 文件里的值比——
    对得上就是 `.env` 供的（可以写），对不上才是外面注入的（写了也不生效，别写）。
    """
    current = os.environ.get(key, "").strip()
    if not current:
        return False
    if not env_path.exists():
        return True
    from ipclick.config_loader.dotenv import parse_env

    try:
        in_file = parse_env(env_path.read_text(encoding="utf-8-sig")).get(key, "")
    except OSError:
        return True
    return current != in_file.strip()


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

    def _running_port(self) -> int:
        """取当前实际在跑的 gRPC 端口，用于解析路径里的 {port}。

        优先用运行时值（它已经算过命令行覆盖），拿不到时退回配置文件。
        """
        actual = self.ctx.runtime_ports.get("SERVER.port")
        if actual:
            return actual
        return ServerSettings.from_config(section(self.ctx.config, "SERVER")).port

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
            tunnel=self._tunnel_state() if tab != "cluster" else None,
        )

    def _tunnel_state(self) -> dict[str, Any]:
        """给「隧道代理接入串」那一格准备回显数据。

        回显的是**占位符形式**：真账号密码在 ``.env`` 里，页面上永远只出现
        ``{IPCLICK_PROXY_AUTH_KEY}`` 这样的名字。这样既能一眼看出"凭据已配好"，
        又不会因为有人截图配置页就把密码漏出去。
        """
        from ipclick.config_loader.env_writer import env_target_path
        from ipclick.secrets import SECRETS, describe_source, proxy_config
        from ipclick.web.proxy_tunnel import TUNNEL_FORMATS, render_masked, render_masked_endpoint

        proxy = section(self.ctx.config, "PROXY")
        merged = proxy_config(self.ctx.config)
        has_credentials = bool(str(merged.get("auth_key") or "").strip())
        scheme = str(proxy.get("scheme") or "http")
        # tunnel_server 在 to_url() 里压过 host/port。它已经不是网页可编辑项了，但手写在
        # toml 里仍然生效——这时必须照着**它**回显，否则页面显示的是 host/port（多半是空的），
        # 而实际在用的是另一个地址：一个会让人查上半天的"页面在说谎"。
        override = str(proxy.get("tunnel_server") or "").strip()
        return {
            "formats": TUNNEL_FORMATS,
            "override": override,
            "value": render_masked_endpoint(scheme, override, with_credentials=has_credentials)
            if override
            else render_masked(
                scheme, str(proxy.get("host") or ""), proxy.get("port") or 0, with_credentials=has_credentials
            ),
            "sources": [
                (spec.label, spec.env, describe_source(self.ctx.config, spec))
                for spec in SECRETS
                if spec.section == "PROXY"
            ],
            "env_path": str(env_target_path()),
        }

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
        updates, errors = parse_form(form)
        tunnel_updates, tunnel_secrets, tunnel_errors = self._parse_tunnel_input(form)
        errors.extend(tunnel_errors)
        if tunnel_updates:
            # 粘进来的整串**压过**下面那几格手填的值：它是这次操作里更明确的意图，
            # 而且拆出来的 scheme/host/port 必须整组一致，让半边被旧值顶掉只会更难查。
            updates.setdefault("PROXY", {}).update(tunnel_updates)
        updates, restart_needed = self._changed_only(updates)

        if "__present__CLUSTER.forward_on" in form:
            desired = "on" if "CLUSTER.forward_on" in form else "off"
            # 只有真的改了才记成一项改动。此前是无条件写入：在集群页什么都不动直接按
            # 保存，也会报"已写回（1 项）"外加"这些项要重启"——那句提示被无谓地打多了，
            # 真需要重启时就没人再看它了。顺带 `if not updates` 在这一页永远不成立。
            if desired != self._current_forward():
                updates.setdefault("CLUSTER", {})["forward"] = desired
                restart_needed.append("服务端转发")

        has_node_grid = tab == "cluster" and any(k.startswith("node_address_") for k in form)
        nodes = parse_nodes(form) if has_node_grid else []
        if has_node_grid:
            errors.extend(validate_nodes(nodes))

        if errors:
            self.ctx.fail(*errors)
            return self.config_page(username, csrf, tab=tab)
        # tunnel_secrets 也要算进"有改动"：只换账号密码时 toml 里一个字都不变，
        # 漏了它就会在真的写了 .env 的情况下报"没有可保存的改动"。
        if not updates and not has_node_grid and not tunnel_secrets:
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
        if tunnel_secrets:
            self._store_proxy_secrets(tunnel_secrets)
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

    def _parse_tunnel_input(self, form: dict[str, str]) -> tuple[dict[str, Any], dict[str, str], list[str]]:
        """把粘进来的隧道接入串拆成 ``[PROXY]`` 更新项和待写入 ``.env`` 的凭据。

        服务商给的那一行里地址和账号密码是混着的。整串塞进 toml 会让密码跟着配置
        文件进版本库，所以拆开：地址进 toml，凭据进 ``.env``。

        解析放在**保存**这一步做，不在浏览器里做——JS 里再写一份解析器，两份迟早
        对不上，而"页面显示解析对了、存进去的是另一回事"是最难查的一类问题。
        """
        raw = (form.get("proxy_tunnel") or "").strip()
        if not raw:
            return {}, {}, []

        from ipclick.web.proxy_tunnel import AUTO_FORMAT, parse_tunnel

        try:
            parsed = parse_tunnel(raw, (form.get("proxy_tunnel_format") or AUTO_FORMAT).strip())
        except ValidationError as e:
            return {}, {}, [str(e)]

        # tunnel_server 清空：端点已经拆进 host/port 了，而 to_url() 里 tunnel_server
        # 的优先级更高——留着旧值会让这次改动整个不生效，且页面上看不出问题在哪。
        updates: dict[str, Any] = {
            "scheme": parsed.scheme,
            "host": parsed.host,
            "port": parsed.port,
            "tunnel_server": "",
        }
        if parsed.credentials_are_placeholders:
            # 用户把回显那串原样交回来了（可能只改了主机），凭据位置还是占位符——
            # 那就是"凭据别动"，不是"把凭据设成 '{IPCLICK_PROXY_AUTH_KEY}' 这个字符串"。
            return updates, {}, []
        return updates, {"IPCLICK_PROXY_AUTH_KEY": parsed.username, "IPCLICK_PROXY_AUTH_PASSWORD": parsed.password}, []

    def _store_proxy_secrets(self, secrets_to_write: dict[str, str]) -> None:
        """把代理凭据写进 ``.env``，并让它对本进程立即生效。"""
        from ipclick.config_loader.env_writer import env_target_path, update_env_file

        target = env_target_path()
        pending = {key: value for key, value in secrets_to_write.items() if not _real_env_overrides(key, target)}
        skipped = sorted(set(secrets_to_write) - set(pending))
        if skipped:
            self.ctx.add_message(
                f"{'、'.join(skipped)} 由真实环境变量提供，没有写进 {target.name}——"
                f"写了也会被环境变量压过去。要改请改注入它的地方。"
            )
        if not pending:
            return

        try:
            path, changed = update_env_file(pending, target)
        except (ConfigError, OSError) as e:
            self.ctx.add_error(f"toml 已保存，但机密没能写进 {target}：{e}")
            return

        for key, value in pending.items():
            # 让「试一试」不用重启就能用上新凭据。空值等于"不设置"，直接删掉这一项——
            # 留一个空串在进程环境里会把 .env 里配好的值悄悄顶掉（见 dotenv 的注释）。
            if value:
                os.environ[key] = value
            else:
                _ = os.environ.pop(key, None)

        cleared = sorted(key for key, value in pending.items() if not value)
        if changed:
            self.ctx.add_message(f"代理凭据已写进 {path}（{len(changed)} 项），本进程已即时生效，不用重启")
        if cleared:
            self.ctx.add_message(f"{'、'.join(cleared)} 被清空了——这个代理现在不带鉴权")
        log.info(f"Web 端写入代理凭据：{'、'.join(sorted(changed)) or '（值未变）'} -> {path}")

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

    def _current_forward(self) -> str:
        """读 [CLUSTER].forward 的当前取值，归一成 on/off。

        判定和渲染复选框时用的是同一条（见 ``_cluster_tab_data``），否则页面显示的状态
        和"算不算改动"会对不上。
        """
        return "on" if str(section(self.ctx.config, "CLUSTER").get("forward", "off")).strip().lower() == "on" else "off"

    def _changed_only(self, updates: dict[str, dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[str]]:
        """筛掉与现有配置相同的项，并按真正变化的字段算出需要重启的标签。"""
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
            from ipclick.config_loader import placeholders
            from ipclick.utils.log_util import LogUtil

            # 和 server.py / cli.main 一样要先解析 {port}：漏了它，
            # [LOG].output = "logs/app-{port}.log" 在这里会按字面路径重开日志，
            # 真正那个 logs/app-9528.log 从此不再收到任何行，而且毫无提示。
            merged = placeholders.resolve_for(
                "LOG", {**section(self.ctx.config, "LOG"), **log_updates}, self._running_port()
            )
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
