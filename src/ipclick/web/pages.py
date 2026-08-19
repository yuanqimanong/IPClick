from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
import json as json_lib
from pathlib import Path
import secrets
import threading
from typing import Any, cast, final

from ipclick.dto.models import METHOD_MAP, IPClickAdapter
from ipclick.dto.proto import task_pb2
from ipclick.exceptions import ConfigError, ValidationError
from ipclick.services.task_service import TaskService
from ipclick.trace import TraceRecord, TraceRecorder
from ipclick.utils.config_util import Settings
from ipclick.utils.log_util import log
from ipclick.web.editable import current_value, groups_for, parse_form, parse_nodes, validate_nodes
from ipclick.web.installer import InstallManager
from ipclick.web.templates import (
    DEFAULT_LIVE_MS,
    LIVE_INTERVALS,
    render_components,
    render_config,
    render_skill,
    render_test,
    render_trace,
    trace_live,
)


TEST_BODY_LIMIT = 256 * 1024

TEST_TIMEOUT_MAX = 120.0

TEST_RETRIES_MAX = 5

TRACE_LIMIT_MAX = 1000

TEST_RESULT_KEEP = 20

SECRET_KEEP = 8

CURL_MAX_LEN = 64 * 1024

REMOTE_COMPONENT_TIMEOUT = 15.0

NODE_PORT_BASE = 19001


@final
class _WebContext:
    def __init__(self) -> None:
        self.code: object = None
        self.details: str = ""

    def set_code(self, code: object) -> None:
        self.code = code

    def set_details(self, details: str) -> None:
        self.details = details

    def is_active(self) -> bool:
        return True

    def invocation_metadata(self) -> tuple[tuple[str, str], ...]:
        return ()


@final
class WebPages:
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
        self.config: Settings = config
        self.runtime_ports: dict[str, int] = dict(runtime_ports or {})
        self.recorder: TraceRecorder = recorder
        self.task_service: TaskService | None = task_service
        self._cluster_snapshot: Any = cluster_snapshot
        self._on_cluster_changed: Callable[[], tuple[bool, str]] | None = on_cluster_changed
        from ipclick.config_loader.writer import target_path

        self.config_path: Path = target_path(config_path, cli_port)
        self._messages: list[str] = []
        self._errors: list[str] = []
        self._test_results: OrderedDict[str, tuple[dict[str, str], dict[str, Any]]] = OrderedDict()
        self._test_lock: threading.Lock = threading.Lock()
        self._generated: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.installer: InstallManager = InstallManager()
        self.installer.on_finished = self._after_install

    def _query_records(self, query: dict[str, str]) -> tuple[list[TraceRecord], str, dict[str, str]]:
        filters = {
            "status": query.get("status", ""),
            "adapter": query.get("adapter", ""),
            "q": query.get("q", ""),
            "limit": query.get("limit", "100"),
        }
        try:
            limit = min(TRACE_LIMIT_MAX, max(1, int(filters["limit"] or 100)))
        except ValueError:
            limit = 100
        filters["limit"] = str(limit)
        records, source = self.recorder.query(
            limit=limit,
            status_class=filters["status"],
            adapter=filters["adapter"],
            keyword=filters["q"],
        )
        return records, source, filters

    def trace_page(self, query: dict[str, str], username: str, csrf: str) -> str:
        records, source, filters = self._query_records(query)
        return render_trace(
            records,
            self.recorder.stats(),
            filters,
            username,
            csrf,
            source=source,
            live_ms=_live_ms(query),
            fragment_url=_fragment_url("/fragment/trace", filters),
        )

    def trace_fragment(self, query: dict[str, str]) -> str:
        records, source, _ = self._query_records(query)
        return trace_live(records, self.recorder.stats(), source=source)

    def trace_json(self, query: dict[str, str]) -> dict[str, Any]:
        records, source, filters = self._query_records(query)
        return {
            "source": source,
            "filters": filters,
            "stats": self.recorder.stats(),
            "records": [
                {
                    "ts": r.ts,
                    "when": r.when,
                    "uuid": r.uuid,
                    "node_id": r.node_id,
                    "adapter": r.adapter,
                    "method": r.method,
                    "url": r.url,
                    "status_code": r.status_code,
                    "duration_ms": r.duration_ms,
                    "size": r.size,
                    "attempts": r.attempts,
                    "forwarded": r.forwarded,
                    "queued_ms": r.queued_ms,
                    "stream": r.stream,
                    "error": r.error,
                }
                for r in records
            ],
        }

    def _adapter_choices(self) -> list[dict[str, Any]]:
        from ipclick.adapters.browser_settings import BrowserSettings
        from ipclick.components import adapter_choices

        return adapter_choices(BrowserSettings.from_config(dict(self.config.get("BROWSER", {}))))

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
        from ipclick.adapters.browser_settings import BrowserSettings

        return render_test(
            form,
            result,
            self._adapter_choices(),
            username,
            csrf,
            nodes=self._target_nodes(),
            curl_notes=curl_notes,
            curl_error=curl_error,
            allow_scripts=BrowserSettings.from_config(dict(self.config.get("BROWSER", {}))).allow_scripts,
        )

    def _target_nodes(self) -> list[dict[str, Any]]:
        service = self.task_service
        cluster = getattr(service, "cluster", None) if service is not None else None
        nodes = list(getattr(cluster, "nodes", ()) or [])
        if not nodes:
            try:
                from ipclick.cluster.node import ClusterConfig

                nodes = list(ClusterConfig.from_config(dict(self.config.get("CLUSTER", {}))).nodes)
            except Exception as e:
                log.debug(f"读集群节点列表失败，「试一试」不显示目标节点：{e}")
                return []

        self_id = self._self_id()
        forwarding = callable(getattr(service, "send_to_node", None))
        return [
            {"id": node.id, "address": node.address, "is_self": node.id == self_id, "forwarding": forwarding}
            for node in nodes
        ]

    def import_curl(self, form: dict[str, str]) -> tuple[dict[str, str], list[str], str]:
        from ipclick.web.curl_parser import parse_curl

        raw = (form.get("curl") or "")[:CURL_MAX_LEN]
        parsed = parse_curl(raw)
        if not parsed.ok:
            return {}, parsed.notes, parsed.error or "解析失败"
        return parsed.as_form(), parsed.notes, ""

    def stash_test_result(self, form: dict[str, str], result: dict[str, Any]) -> str:
        token = secrets.token_urlsafe(9)
        with self._test_lock:
            self._test_results[token] = (dict(form), result)
            while len(self._test_results) > TEST_RESULT_KEEP:
                _ = self._test_results.popitem(last=False)
        return token

    def take_test_result(self, token: str) -> tuple[dict[str, str], dict[str, Any] | None]:
        if not token:
            return {}, None
        with self._test_lock:
            entry = self._test_results.get(token)
        return entry if entry is not None else ({}, None)

    def run_test(self, form: dict[str, str]) -> dict[str, Any]:
        if self.task_service is None:
            return {"error_only": True, "error": "本实例没有可用的任务服务（Web 端以只读方式启动）"}

        url = (form.get("url") or "").strip()
        if not url:
            return {"error_only": True, "error": "请填一个网址"}
        if not url.startswith(("http://", "https://")):
            return {"error_only": True, "error": "网址必须以 http:// 或 https:// 开头"}

        try:
            request = self._build_request(form, url)
        except ValidationError as e:
            return {"error_only": True, "error": str(e)}

        target = (form.get("target_node") or "").strip()
        response: task_pb2.TaskResp
        try:
            if target:
                response = self._send_to_node(request, target)
            else:
                sender = getattr(self.task_service, "send_from_thread", None)
                if callable(sender):
                    response = cast("task_pb2.TaskResp", sender(request, cast(Any, _WebContext())))
                else:
                    response = self.task_service.Send(request, cast(Any, _WebContext()))
        except Exception as e:
            log.exception(f"试一试请求失败：{e}")
            return {"error_only": True, "error": _readable_error(e, target)}

        body = response.content
        shown = body[:TEST_BODY_LIMIT]
        return {
            "status_code": response.status_code,
            "effective_url": response.effective_url,
            "elapsed_ms": response.response_time_ms,
            "size": len(body),
            "shown": len(shown),
            "truncated": len(body) > len(shown),
            "headers": dict(response.response_headers),
            "error": response.error_message,
            "body": shown.decode("utf-8", errors="replace"),
            "trace": {
                "node_id": response.trace.node_id,
                "adapter": response.trace.adapter,
                "attempts": response.trace.attempts,
                "forwarded": response.trace.forwarded,
                "queued_ms": response.trace.queued_ms,
            }
            if response.HasField("trace")
            else {},
        }

    def _send_to_node(self, request: task_pb2.ReqTask, node_id: str) -> task_pb2.TaskResp:
        service = self.task_service
        sender = getattr(service, "send_to_node", None)
        if callable(sender):
            return cast(task_pb2.TaskResp, sender(request, node_id))
        return self._direct_send(request, node_id)

    def _direct_send(self, request: task_pb2.ReqTask, node_id: str) -> task_pb2.TaskResp:
        deadline = request.timeout_seconds * (request.max_retries + 1) + 15
        return cast(
            task_pb2.TaskResp,
            self._call_node(
                node_id,
                lambda stub, metadata, timeout: stub.Send(request, timeout=timeout, metadata=metadata),
                timeout=deadline,
            ),
        )

    def _build_request(self, form: dict[str, str], url: str) -> task_pb2.ReqTask:
        adapter_name = (form.get("adapter") or "").strip()
        try:
            adapter = IPClickAdapter.from_str(adapter_name) if adapter_name else IPClickAdapter.CURL_CFFI
        except ValueError as e:
            raise ValidationError(str(e)) from e

        method_name = (form.get("method") or "GET").strip().upper()
        method_value = next((v for v, name in METHOD_MAP.items() if name == method_name), None)
        if method_value is None:
            raise ValidationError(f"不支持的方法 {method_name!r}")

        timeout = max(1.0, min(TEST_TIMEOUT_MAX, _as_number(form.get("timeout"), 30.0, "超时")))

        retries = int(max(0, min(TEST_RETRIES_MAX, _as_number(form.get("max_retries"), 0, "重试次数"))))

        request = task_pb2.ReqTask(
            uuid=f"web-{method_name.lower()}",
            adapter=cast(Any, adapter.pb_value),
            method=cast(Any, method_value),
            url=url,
            timeout_seconds=timeout,
            max_retries=retries,
            verify_ssl=form.get("verify") == "on",
            allow_redirects=form.get("allow_redirects") == "on",
        )
        if retries:
            request.retry_backoff_seconds = max(0.0, _as_number(form.get("retry_backoff"), 1.0, "重试退避"))

        if adapter is IPClickAdapter.CURL_CFFI:
            request.impersonate = (form.get("impersonate") or "").strip()

        self._apply_body(request, form)
        self._apply_maps(request, form)

        proxy = self._resolve_proxy(form)
        if proxy:
            request.proxy = proxy

        codes = _parse_status_codes(form.get("allowed_status_codes"))
        if codes:
            request.allowed_status_codes.extend(codes)

        if config_json := (form.get("automation_config") or "").strip():
            try:
                _ = json_lib.loads(config_json)
            except ValueError as e:
                raise ValidationError(f"自动化配置不是合法 JSON：{e}") from e
            request.automation_config = config_json
        if script := (form.get("automation_script") or "").strip():
            request.automation_script = script

        return request

    @staticmethod
    def _apply_body(request: task_pb2.ReqTask, form: dict[str, str]) -> None:
        body = form.get("body") or ""
        if not body.strip():
            return
        if (form.get("body_kind") or "raw") == "json":
            try:
                _ = json_lib.loads(body)
            except ValueError as e:
                raise ValidationError(f"请求体选了 JSON 但内容不是合法 JSON：{e}") from e
            request.json = body
            return
        request.data = body.encode("utf-8")

    @staticmethod
    def _apply_maps(request: task_pb2.ReqTask, form: dict[str, str]) -> None:
        for line in (form.get("headers") or "").splitlines():
            name, sep, value = line.partition(":")
            if sep and name.strip():
                request.headers[name.strip()] = value.strip()
        for line in (form.get("cookies") or "").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip():
                request.cookies[name.strip()] = value.strip()
        pairs: dict[str, str] = {}
        for line in (form.get("params") or "").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip():
                pairs[name.strip()] = value.strip()
        if pairs:
            request.params = json_lib.dumps(pairs, ensure_ascii=False)

    def _resolve_proxy(self, form: dict[str, str]) -> str:
        mode = (form.get("proxy_mode") or "none").strip()
        if mode == "custom":
            return (form.get("proxy_url") or "").strip()
        if mode != "config":
            return ""
        from ipclick.dto.models import ProxyConfig
        from ipclick.secrets import proxy_config

        resolved = ProxyConfig(**proxy_config(self.config)).to_url()
        if not resolved:
            raise ValidationError("选了「用配置文件里的 [PROXY]」，但那一节没有配 host / tunnel_server")
        return resolved

    def components_page(self, username: str, csrf: str, *, node_id: str = "") -> str:
        from ipclick.adapters.browser_engines import playwright_registry_dir
        from ipclick.adapters.browser_settings import BrowserSettings
        from ipclick.components import COMPONENTS, snapshot
        from ipclick.web.installer import browser_body_location, detect_toolchain

        messages, errors = self._take_flash()
        nodes = self._target_nodes()

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
            snapshot(BrowserSettings.from_config(dict(self.config.get("BROWSER", {})))),
            username,
            csrf,
            toolchain=toolchain.describe() if toolchain else "既没有 pip 也没有 uv —— 无法从页面安装",
            job=self.installer.current(),
            messages=messages,
            errors=errors,
            bodies=bodies,
            registry_dir=str(registry) if registry else "~/.cache/ms-playwright",
            nodes=nodes,
            active_node="",
            remote=False,
        )

    def skill_markdown(self) -> str:
        from ipclick import skill

        return skill.markdown()

    def skill_page(self, username: str, csrf: str) -> str:
        from ipclick import __version__, skill

        return render_skill(
            self.skill_markdown(),
            username,
            csrf,
            version=__version__,
            description=skill.description(),
            install_dir=str(skill.DEFAULT_INSTALL_DIR / skill.SKILL_NAME),
        )

    def remote_component(self, node_id: str, op: str, extra: str = "", browser_kind: str = "") -> dict[str, Any]:
        import json as json_lib

        import grpc

        from ipclick.dto.proto import task_pb2

        try:
            response = self._call_node(
                node_id,
                lambda stub, metadata, timeout: stub.Component(
                    task_pb2.ComponentReq(
                        op=op,
                        extra=extra,
                        browser_kind=browser_kind,
                        from_node=self._self_id(),
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

    def _call_node(self, node_id: str, call: Callable[[Any, Any, float], Any], *, timeout: float) -> Any:
        import grpc

        from ipclick.auth import build_client_metadata
        from ipclick.cluster.node import ClusterConfig
        from ipclick.cluster.tokens import cluster_secret, token_for
        from ipclick.dto.proto import task_pb2_grpc
        from ipclick.sdk import CHANNEL_OPTIONS
        from ipclick.tls import TLSSettings, channel_credentials, channel_options

        cluster_section = dict(self.config.get("CLUSTER", {}))
        parsed = ClusterConfig.from_config(cluster_section)
        node = next((n for n in parsed.nodes if n.id == node_id), None)
        if node is None:
            raise ValidationError(f"节点 {node_id!r} 不在集群节点列表里，已有：{[n.id for n in parsed.nodes]}")

        tls = TLSSettings.from_config(dict(self.config.get("SECURITY", {})))
        token = token_for(node.id, node.token, cluster_secret(cluster_section))
        options = [*CHANNEL_OPTIONS, *channel_options(tls)]
        channel = (
            grpc.secure_channel(node.address, channel_credentials(tls), options=options)
            if tls.enabled
            else grpc.insecure_channel(node.address, options=options)
        )
        try:
            return call(task_pb2_grpc.TaskServiceStub(channel), build_client_metadata(token), timeout)
        finally:
            channel.close()

    def component_action(self, op: str, extra: str, node_id: str = "") -> tuple[bool, str]:
        from ipclick.adapters.browser_settings import BrowserSettings

        kind = BrowserSettings.from_config(dict(self.config.get("BROWSER", {}))).kind
        if node_id:
            remote_op = "browser" if op == "fetch" else op
            result = self.remote_component(node_id, remote_op, extra, kind)
            return bool(result["ok"]), str(result["message"])

        if op == "install":
            return self.installer.install(extra)
        if op == "uninstall":
            return self.installer.uninstall(extra)
        if op == "fetch":
            return self.installer.fetch_browser(extra, kind)
        return False, f"未知操作 {op!r}"

    def component_status(self, node_id: str = "") -> dict[str, Any] | None:
        if not node_id:
            return self.installer.current()
        return self.remote_component(node_id, "status").get("job")

    def refresh_components(self) -> tuple[bool, str]:
        from ipclick.adapters import registry

        registry.refresh()
        return True, "已重新探测各组件的安装状态"

    def _after_install(self, job: Any) -> None:
        from ipclick.adapters import registry

        registry.refresh()
        log.debug(f"依赖任务 {getattr(job, 'title', '')} 结束，已刷新组件状态与适配器注册表")

    def _groups(self, tab: str = "basic") -> list[tuple[str, list[dict[str, Any]]]]:
        groups: list[tuple[str, list[dict[str, Any]]]] = []
        for title, fields in groups_for(tab):
            items = [
                {
                    "name": field.name,
                    "label": field.label,
                    "kind": field.kind,
                    "value": current_value(self.config, field),
                    "choices": field.choices,
                    "hint": field.hint,
                    "restart": field.restart,
                    "running": self._running_mismatch(field.name, current_value(self.config, field)),
                }
                for field in fields
            ]
            groups.append((title, items))
        return groups

    def _running_mismatch(self, name: str, file_value: Any) -> int:
        actual = self.runtime_ports.get(name)
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

        security = dict(self.config.get("SECURITY", {}))
        rows: list[tuple[str, Any]] = [
            ("传输层 [SECURITY.tls]", esc(describe(TLSSettings.from_config(security)))),
            ("令牌鉴权 [SECURITY].auth_token", "已配置" if load_tokens(security) else "未配置"),
            ("拦截内网地址", esc(security.get("block_private_networks", False))),
            ("拦截元数据端点", esc(security.get("block_metadata_endpoints", True))),
            ("允许的协议", esc(", ".join(security.get("allowed_schemes", ["http", "https"])))),
            (
                "允许页内 JS [BROWSER].allow_scripts",
                esc(dict(self.config.get("BROWSER", {})).get("allow_scripts", False)),
            ),
        ]
        rows.extend((f"机密 {spec.label}", esc(describe_source(self.config, spec))) for spec in SECRETS)
        return rows

    def _generators(self) -> list[dict[str, Any]]:
        from ipclick.secrets import SECRETS, describe_source

        return [
            {
                "env": spec.env,
                "label": spec.label,
                "shared": spec.shared,
                "note": spec.note,
                "source": describe_source(self.config, spec),
            }
            for spec in SECRETS
            if spec.generatable
        ]

    def config_page(self, username: str, csrf: str, *, generated_token: str = "", tab: str = "basic") -> str:
        messages, errors = self._take_flash()
        return render_config(
            self._groups(tab),
            username,
            csrf,
            config_path=str(self.config_path),
            messages=messages,
            errors=errors,
            readonly_note=self._readonly(),
            generators=self._generators(),
            generated=self.take_generated(generated_token),
            tab=tab,
            cluster=self._cluster_tab_data() if tab == "cluster" else None,
        )

    def _next_node_port(self) -> int:
        used = {int(port) for node in self._nodes() if (port := node["address"].rpartition(":")[2]).isdigit()}
        for candidate in range(NODE_PORT_BASE, NODE_PORT_BASE + 10000):
            if candidate not in used:
                return candidate
        return NODE_PORT_BASE

    def _cluster_tab_data(self) -> dict[str, Any]:
        from ipclick.auth import load_tokens
        from ipclick.cluster.tokens import cluster_secret

        cluster_section = dict(self.config.get("CLUSTER", {}))
        return {
            "forward": str(cluster_section.get("forward", "off")).strip().lower() == "on",
            "nodes": self._nodes(),
            "auth_configured": bool(load_tokens(dict(self.config.get("SECURITY", {})))),
            "secret_configured": bool(cluster_secret(cluster_section)),
            "next_port": self._next_node_port(),
        }

    def _deploy_plans(self) -> list[Any]:
        from ipclick.auth import load_tokens
        from ipclick.cluster.tokens import cluster_secret
        from ipclick.web.deploy import build_plan

        cluster_section = dict(self.config.get("CLUSTER", {}))
        tokens = load_tokens(dict(self.config.get("SECURITY", {})))
        nodes = [{"id": n["id"], "address": n["address"]} for n in self._nodes() if n.get("address")]
        forward = str(cluster_section.get("forward", "off")).strip().lower() == "on"
        max_workers = int(dict(self.config.get("SERVER", {})).get("max_workers", 100) or 100)
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

    def deploy_plan(self, node_id: str) -> Any | None:
        return next((plan for plan in self._deploy_plans() if plan.node_id == node_id), None)

    def deploy_page(self, node_id: str, username: str, csrf: str) -> str | None:
        from ipclick.web.templates import render_deploy

        plans = self._deploy_plans()
        plan = next((p for p in plans if p.node_id == node_id), None)
        if plan is None:
            return None
        return render_deploy(plan.snapshot(), username, csrf, total_nodes=len(plans))

    def deploy_bundle(self) -> bytes:
        from ipclick.web.deploy import bundle

        return bundle(self._deploy_plans())

    def generate_secret(self, env: str) -> str:
        from ipclick.secrets import SECRETS
        from ipclick.web.auth import generate_password

        spec = next((s for s in SECRETS if s.env == env and s.generatable), None)
        if spec is None:
            self._errors = [f"{env!r} 不是可生成的凭据"]
            return ""

        token = secrets.token_urlsafe(9)
        with self._test_lock:
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
        if not token:
            return None
        with self._test_lock:
            return self._generated.pop(token, None)

    def save_config(self, form: dict[str, str], username: str, csrf: str) -> str:
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
            self._errors = errors
            return self.config_page(username, csrf, tab=tab)
        if not updates and not has_node_grid:
            self._errors = ["没有可保存的改动"]
            return self.config_page(username, csrf, tab=tab)

        try:
            text = self._read_config_text()
            new_text, changes = set_values(text, updates) if updates else (text, [])
            if has_node_grid:
                new_text = set_nodes(new_text, self._preserve_node_fields(nodes))
            _ = save(self.config_path, new_text)
        except (ConfigError, OSError) as e:
            self._errors = [str(e)]
            return self.config_page(username, csrf, tab=tab)

        self._messages = [f"已写回 {self.config_path}（{len(changes)} 项）"]
        if has_node_grid:
            self._messages[0] += f"，{len(nodes)} 个节点"
        if restart_needed:
            self._messages.append("这些项要重启 ipclick 才生效：" + "、".join(sorted(set(restart_needed))))
        self._apply_live(updates)
        log.info(f"Web 端保存配置：{'; '.join(changes) or '（仅节点）'}")
        self._reload_config()
        if tab == "cluster":
            self._hot_reload_cluster()
        return self.config_page(username, csrf, tab=tab)

    def add_node(self, form: dict[str, str], username: str, csrf: str) -> str:
        from ipclick.config_loader.writer import save, set_nodes

        host = (form.get("new_node_host") or "").strip().strip("[]")
        if not host:
            self._errors = ["请填 IP 或主机名"]
            return self.config_page(username, csrf, tab="cluster")
        try:
            port = int((form.get("new_node_port") or "").strip() or self._next_node_port())
        except ValueError:
            self._errors = [f"端口必须是数字，收到 {form.get('new_node_port')!r}"]
            return self.config_page(username, csrf, tab="cluster")

        address = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        node_id = (form.get("new_node_id") or "").strip() or address
        try:
            weight = max(1, int((form.get("new_node_weight") or "100").strip() or 100))
        except ValueError:
            weight = 100

        existing = self._nodes()
        if any(n["id"] == node_id for n in existing):
            self._errors = [f"已经有一个 id 为 {node_id!r} 的节点了"]
            return self.config_page(username, csrf, tab="cluster")

        nodes = [{"id": n["id"], "address": n["address"], "weight": n["weight"]} for n in existing]
        nodes.append({"id": node_id, "address": address, "weight": weight})
        if errors := validate_nodes(nodes):
            self._errors = errors
            return self.config_page(username, csrf, tab="cluster")

        try:
            _ = save(self.config_path, set_nodes(self._read_config_text(), self._preserve_node_fields(nodes)))
        except (ConfigError, OSError) as e:
            self._errors = [str(e)]
            return self.config_page(username, csrf, tab="cluster")

        self._messages = [f"已添加节点 {node_id}（{address}）"]
        log.info(f"Web 端添加节点：{node_id} -> {address}")
        self._reload_config()
        self._hot_reload_cluster()
        return self.config_page(username, csrf, tab="cluster")

    def _changed_only(
        self, updates: dict[str, dict[str, Any]], restart_needed: list[str]
    ) -> tuple[dict[str, dict[str, Any]], list[str]]:
        from ipclick.web.editable import FIELDS

        changed: dict[str, dict[str, Any]] = {}
        labels: list[str] = []
        for section, entries in updates.items():
            for key, value in entries.items():
                field = FIELDS.get(f"{section}.{key}")
                current = current_value(self.config, field) if field is not None else None
                if field is not None and _same_value(current, value):
                    continue
                changed.setdefault(section, {})[key] = value
                if field is not None and field.restart:
                    labels.append(field.label)
        for label in restart_needed:
            if label == "服务端转发" and "CLUSTER" in changed and "forward" in changed["CLUSTER"]:
                labels.append(label)
        return changed, labels

    def remove_node(self, form: dict[str, str], username: str, csrf: str) -> str:
        from ipclick.config_loader.writer import save, set_nodes

        node_id = (form.get("remove_node") or "").strip()
        remaining = [
            {"id": n["id"], "address": n["address"], "weight": n["weight"]} for n in self._nodes() if n["id"] != node_id
        ]
        if len(remaining) == len(self._nodes()):
            self._errors = [f"没有 id 为 {node_id!r} 的节点"]
            return self.config_page(username, csrf, tab="cluster")

        try:
            _ = save(self.config_path, set_nodes(self._read_config_text(), self._preserve_node_fields(remaining)))
        except (ConfigError, OSError) as e:
            self._errors = [str(e)]
            return self.config_page(username, csrf, tab="cluster")

        self._messages = [f"已移除节点 {node_id}（只改了本机的节点列表，那台机器还在跑）"]
        log.info(f"Web 端移除节点：{node_id}")
        self._reload_config()
        self._hot_reload_cluster()
        return self.config_page(username, csrf, tab="cluster")

    def _preserve_node_fields(self, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        preserved = {n.id: n for n in self._existing_nodes()}
        for node in nodes:
            old = preserved.get(str(node["id"]))
            if old is None:
                continue
            for key in ("token", "region", "zone"):
                if value := getattr(old, key, ""):
                    node[key] = value
        return nodes

    def _hot_reload_cluster(self) -> None:
        if self._on_cluster_changed is None:
            return
        try:
            ok, message = self._on_cluster_changed()
        except Exception as e:
            log.exception(f"集群配置热更新失败：{e}")
            self._errors.append(f"已写回文件，但热更新失败（重启后仍会生效）：{type(e).__name__}: {e}")
        else:
            (self._messages if ok else self._errors).append(message)

    def _apply_live(self, updates: dict[str, dict[str, Any]]) -> None:
        log_updates = updates.get("LOG") or {}
        debug = bool((updates.get("GENERAL") or {}).get("debug", False))
        if log_updates.get("level") or "debug" in (updates.get("GENERAL") or {}):
            from ipclick.utils.log_util import LogUtil

            merged = {**dict(self.config.get("LOG", {})), **log_updates}
            LogUtil.init_from_config(merged, debug=debug)
            log.info(f"日志级别已即时切换为 {'DEBUG' if debug else merged.get('level', 'info')}")

        trace_updates = updates.get("TRACE") or {}
        if "memory_size" in trace_updates or "only_errors" in trace_updates or "record_url" in trace_updates:
            from dataclasses import replace

            self.recorder.settings = replace(
                self.recorder.settings,
                memory_size=int(trace_updates.get("memory_size", self.recorder.settings.memory_size)),
                only_errors=bool(trace_updates.get("only_errors", self.recorder.settings.only_errors)),
                record_url=bool(trace_updates.get("record_url", self.recorder.settings.record_url)),
            )

    def _nodes(self) -> list[dict[str, Any]]:
        from ipclick.cluster.node import ClusterConfig
        from ipclick.cluster.tokens import cluster_secret

        section = dict(self.config.get("CLUSTER", {}))
        cluster = ClusterConfig.from_config(section)
        secret = cluster_secret(section)
        self_id = self._self_id()
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

    def probe_node(self, node_id: str, address: str = "") -> dict[str, Any]:
        from ipclick.cluster.node import ClusterConfig, Node
        from ipclick.cluster.probe import probe_node as run_probe
        from ipclick.cluster.tokens import cluster_secret
        from ipclick.tls import TLSSettings

        section = dict(self.config.get("CLUSTER", {}))
        cluster = ClusterConfig.from_config(section)
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
            secret=cluster_secret(section),
            tls=TLSSettings.from_config(dict(self.config.get("SECURITY", {}))),
            from_node=self._self_id(),
        )
        return {
            "ok": result.ok,
            "warn": result.ok and result.remote_auth_required is False,
            "title": _probe_title(result),
            "detail": f"{result.detail}（{result.elapsed_ms} ms）",
            "elapsed_ms": result.elapsed_ms,
            "remote_version": result.remote_version,
        }

    def _self_id(self) -> str:
        service = self.task_service
        return str(getattr(service, "self_id", "") or getattr(service, "node_id", "") or "")

    def _existing_nodes(self) -> tuple[Any, ...]:
        from ipclick.cluster.node import ClusterConfig

        return ClusterConfig.from_config(dict(self.config.get("CLUSTER", {}))).nodes

    def _read_config_text(self) -> str:
        if self.config_path.exists():
            return self.config_path.read_text(encoding="utf-8")
        from ipclick.config_loader.loader import example_config

        log.info(f"{self.config_path} 不存在，将以默认模板为基础创建")
        return example_config()

    def _reload_config(self) -> None:
        from ipclick.config_loader.loader import load_config

        try:
            load_config.cache_clear()
            self.config = load_config(str(self.config_path) if self.config_path.exists() else None)
        except Exception as e:
            log.warning(f"重新加载配置失败：{e}")

    def _take_flash(self) -> tuple[list[str], list[str]]:
        messages, errors = self._messages, self._errors
        self._messages, self._errors = [], []
        return messages, errors

    def dashboard_extras(self) -> dict[str, Any]:
        from ipclick.adapters.browser_engines import resolve_engine
        from ipclick.adapters.browser_settings import BrowserSettings
        from ipclick.components import snapshot

        browser = BrowserSettings.from_config(dict(self.config.get("BROWSER", {})))
        try:
            active = resolve_engine(browser.engine) if browser.enabled else ""
        except Exception:
            active = browser.engine
        return {
            "trace": self.recorder.stats(),
            "recent": self.recorder.recent(limit=12),
            "components": snapshot(browser),
            "active_engine": active,
            "config_path": str(self.config_path),
        }


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
        return f"节点 {node_id} 的版本低于 0.5，没有远程组件管理接口——先把那台升级上来"
    if code is grpc.StatusCode.UNAUTHENTICATED:
        return f"节点 {node_id} 鉴权不通过：两端的 IPCLICK_CLUSTER_SECRET 必须完全一致。{details}"
    if code is grpc.StatusCode.UNAVAILABLE:
        return f"连不上节点 {node_id}：{details}"
    name = getattr(code, "name", str(code))
    return f"节点 {node_id} 返回 {name}{f'：{details}' if details else ''}"


def _same_value(current: Any, new: Any) -> bool:
    if isinstance(current, bool) or isinstance(new, bool):
        return isinstance(current, bool) and isinstance(new, bool) and current == new
    if isinstance(current, (int, float)) and isinstance(new, (int, float)):
        return float(current) == float(new)
    return current == new


def _as_number(raw: str | None, default: float, label: str) -> float:
    text = (raw or "").strip()
    if not text:
        return default
    try:
        return float(text)
    except ValueError as e:
        raise ValidationError(f"{label}必须是数字，收到 {text!r}") from e


def _parse_status_codes(raw: str | None) -> list[int]:
    text = (raw or "").strip()
    if not text:
        return []
    codes: list[int] = []
    for chunk in text.replace("，", ",").split(","):
        item = chunk.strip()
        if not item:
            continue
        if not item.isdigit():
            raise ValidationError(f"允许的状态码里有非数字项：{item!r}")
        codes.append(int(item))
    return codes


def _readable_error(error: Exception, target_node: str = "") -> str:
    import grpc

    if isinstance(error, grpc.RpcError):
        code = getattr(error, "code", lambda: None)()
        name = getattr(code, "name", str(code))
        details = (getattr(error, "details", lambda: "")() or "").strip()
        message = f"{name}：{details}" if details else str(name)
        if target_node:
            message = f"转发到节点 {target_node} 失败 —— {message}"
            if code is grpc.StatusCode.UNAUTHENTICATED:
                message += (
                    "。集群内部鉴权不通过：两端的 IPCLICK_CLUSTER_SECRET 必须完全一致"
                    "（在一台机器上生成，原样复制到其余机器的 .env）"
                )
            elif code is grpc.StatusCode.UNAVAILABLE:
                message += "。目标节点连不上——先用节点页的「测试连接」确认它起来了没"
        return message
    return f"{type(error).__name__}: {error}"


def _probe_title(result: Any) -> str:
    if not result.reachable:
        return "连不上"
    if result.authenticated is False:
        return "鉴权不通过"
    if result.remote_auth_required is False:
        return "通过（对方未设防）"
    return "通过"


def _live_ms(query: dict[str, str]) -> int:
    if "_" not in query:
        return DEFAULT_LIVE_MS
    raw = query.get("live", "")
    if raw == "":
        return 0
    if raw == "1":
        return DEFAULT_LIVE_MS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_LIVE_MS
    return value if any(value == ms for ms, _, _ in LIVE_INTERVALS) else DEFAULT_LIVE_MS


def _fragment_url(base: str, filters: dict[str, str]) -> str:
    from urllib.parse import urlencode

    query = {k: v for k, v in filters.items() if v}
    return f"{base}?{urlencode(query)}" if query else base


__all__ = ["TEST_BODY_LIMIT", "WebPages"]
