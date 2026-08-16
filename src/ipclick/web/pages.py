"""请求流 / 试一试 / 组件 / 配置 / 节点 这几页的业务逻辑。

从 :mod:`ipclick.web.server` 拆出来：那个模块负责 HTTP（路由、会话、CSRF、
响应头），这个负责"页面要展示什么、提交上来要怎么处理"。混在一起的话，
每加一页都得往 HTTP 处理器里塞一段业务代码，很快就没法看了。

这一层刻意只依赖注入进来的对象（TaskService、记录器、配置），不自己去
import 服务端——这样它在测试里可以单独构造。
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
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
from ipclick.web.editable import GROUPS, current_value, parse_form, parse_nodes, validate_nodes
from ipclick.web.installer import InstallManager
from ipclick.web.templates import (
    render_components,
    render_config,
    render_nodes,
    render_test,
    render_trace,
    trace_live,
)


#: "试一试"页面最多显示多少字节源码。再多的话浏览器渲染会卡，
#: 而看源码这件事看前几十 KB 基本就够判断了。
TEST_BODY_LIMIT = 256 * 1024

#: "试一试"页面允许的超时上限（秒）。页面是同步等结果的，
#: 让它能等十分钟等于给自己留一个占满 worker 的口子。
TEST_TIMEOUT_MAX = 120.0

#: 请求流一页最多多少条。
TRACE_LIMIT_MAX = 1000

#: 「试一试」的结果最多暂存几条（Post/Redirect/Get 用）。
#: 只是为了让重定向后的那一次 GET 能取到，不是历史记录——历史看请求流。
TEST_RESULT_KEEP = 20

#: 生成的机密最多暂存几条。取完即弃，这个数只是防止有人狂点生成把内存占满。
SECRET_KEEP = 8

#: 粘贴的 curl 命令长度上限。DevTools 导出的最长也就几 KB，
#: 给到 64 KB 足够宽松，同时挡住"往这里贴一个文件"。
CURL_MAX_LEN = 64 * 1024


@final
class _WebContext:
    """ "试一试"用的假 ServicerContext。

    只收状态码，不把错误传播到别处——页面自己会把 error_message 显示出来。
    """

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
        # 刻意不带转发标记：从页面发的请求就该和外部调用方一样，
        # 该分发就分发——否则"试一试"验不出集群到底通不通。
        return ()


@final
class WebPages:
    """Web 端各页面的数据与操作。"""

    def __init__(
        self,
        config: Settings,
        recorder: TraceRecorder,
        *,
        task_service: TaskService | None = None,
        config_path: str | Path | None = None,
        cluster_snapshot: Any = None,
        on_cluster_changed: Callable[[], tuple[bool, str]] | None = None,
    ):
        self.config: Settings = config
        self.recorder: TraceRecorder = recorder
        self.task_service: TaskService | None = task_service
        self._cluster_snapshot: Any = cluster_snapshot
        #: 保存节点之后调一次，让改动**立即生效**而不是等重启。由服务端注入
        #: （它才有权重建转发路由与观测池）。
        self._on_cluster_changed: Callable[[], tuple[bool, str]] | None = on_cluster_changed
        from ipclick.config_loader.writer import target_path

        self.config_path: Path = target_path(config_path)
        #: 上一次保存的结果，渲染完就清掉（POST 之后直接渲染，不做重定向——
        #: 重定向就得把消息塞进会话或 URL，前者要加状态、后者会被复制粘贴传播）
        self._messages: list[str] = []
        self._errors: list[str] = []
        #: 「试一试」的结果暂存区（Post/Redirect/Get 用）。
        #:
        #: POST 完直接渲染的话，用户按 F5 就会把整次请求重新提交一遍——而这一页
        #: 的一次提交可能是几十秒的真实浏览器渲染。等久了按 F5 恰恰是最常见的动作。
        #: 只留最近几条，按 token 取，取完即弃。
        self._test_results: OrderedDict[str, tuple[dict[str, str], dict[str, Any]]] = OrderedDict()
        self._test_lock: threading.Lock = threading.Lock()
        #: 刚生成的机密。**取完即弃**——"只显示一次"这件事就是靠这个 pop 保证的，
        #: 服务端不留任何副本。
        self._generated: OrderedDict[str, dict[str, Any]] = OrderedDict()
        #: 依赖安装任务。装完自动刷新探测缓存与适配器注册表。
        self.installer: InstallManager = InstallManager()
        self.installer.on_finished = self._after_install

    # ------------------------------------------------------------------ #
    # 请求流
    # ------------------------------------------------------------------ #

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
        # 默认开实时刷新：这一页存在的意义就是看着请求打进来。
        # 只有显式提交过表单（带 _ 标记）且没勾选时才关掉。
        live = query.get("live") == "1" or "_" not in query
        return render_trace(
            records,
            self.recorder.stats(),
            filters,
            username,
            csrf,
            source=source,
            live=live,
            fragment_url=_fragment_url("/fragment/trace", filters),
        )

    def trace_fragment(self, query: dict[str, str]) -> str:
        """请求流里自动刷新的那一块。前端只换这一块的 innerHTML。

        和整页走**同一个**渲染函数，所以不会出现"局部刷新出来的表格和整页
        渲染的不一样"——那种失步只有在数据变化时才暴露，最难查。
        """
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

    # ------------------------------------------------------------------ #
    # 试一试
    # ------------------------------------------------------------------ #

    def _adapter_choices(self) -> list[dict[str, Any]]:
        """「试一试」下拉框的分组数据。

        0.3 这里只返回注册表里**本机已装**的那几个，于是没装的适配器直接从下拉框
        消失——对着 wiki 看的人会觉得"文档和实现对不上"，也不知道 IPClick 到底
        支持哪些。现在全部列出，没装的置灰并标上安装命令。
        """
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
        return render_test(
            form,
            result,
            self._adapter_choices(),
            username,
            csrf,
            nodes=self._target_nodes(),
            curl_notes=curl_notes,
            curl_error=curl_error,
        )

    def _target_nodes(self) -> list[dict[str, Any]]:
        """能点名的节点。只有开了服务端转发才有意义——没开的话本进程压根不转发，
        选了也只会打到自己身上，那个下拉框纯属误导。
        """
        service = self.task_service
        if service is None or not hasattr(service, "send_to_node"):
            return []
        cluster = getattr(service, "cluster", None)
        if cluster is None:
            return []
        self_id = self._self_id()
        return [
            {"id": node.id, "address": node.address, "is_self": node.id == self_id}
            for node in getattr(cluster, "nodes", ())
        ]

    def import_curl(self, form: dict[str, str]) -> tuple[dict[str, str], list[str], str]:
        """把粘贴的 curl 命令解析成表单。返回 ``(表单, 提示, 错误)``。"""
        from ipclick.web.curl_parser import parse_curl

        raw = (form.get("curl") or "")[:CURL_MAX_LEN]
        parsed = parse_curl(raw)
        if not parsed.ok:
            return {}, parsed.notes, parsed.error or "解析失败"
        return parsed.as_form(), parsed.notes, ""

    def stash_test_result(self, form: dict[str, str], result: dict[str, Any]) -> str:
        """存下一次「试一试」的结果，返回取回它的 token。"""
        token = secrets.token_urlsafe(9)
        with self._test_lock:
            self._test_results[token] = (dict(form), result)
            while len(self._test_results) > TEST_RESULT_KEEP:
                _ = self._test_results.popitem(last=False)
        return token

    def take_test_result(self, token: str) -> tuple[dict[str, str], dict[str, Any] | None]:
        """按 token 取回结果。取不到就当成一次普通的打开（空表单）。"""
        if not token:
            return {}, None
        with self._test_lock:
            entry = self._test_results.get(token)
        return entry if entry is not None else ({}, None)

    def run_test(self, form: dict[str, str]) -> dict[str, Any]:
        """就地发一次请求。

        走本进程 TaskService 的 ``Send``——和真实 gRPC 调用方**同一条**路径，
        因此 SSRF 准入、限流、集群转发、链路记录全都照常生效。另写一条只在页面上
        成立的路径毫无意义：那验证的就不是线上行为了。

        指定了"目标节点"时改走 ``send_to_node``：跳过负载均衡直连那一台。
        这是唯一的例外，而它的目的恰恰是"验证某台机器配对没有"——按策略选就
        只能靠轮询碰运气命中，节点一多完全没法用。
        """
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
        try:
            if target:
                response = self._send_to_node(request, target)
            else:
                response = self.task_service.Send(request, cast(Any, _WebContext()))
        except Exception as e:  # 页面不该因为一次试探而 500
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
        if not callable(sender):
            raise ValidationError('本节点没有开启服务端转发（[CLUSTER].forward = "on"），无法指定目标节点')
        return cast(task_pb2.TaskResp, sender(request, node_id))

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

        try:
            timeout = float(form.get("timeout") or 30)
        except ValueError as e:
            raise ValidationError("超时必须是数字") from e
        timeout = max(1.0, min(TEST_TIMEOUT_MAX, timeout))

        request = task_pb2.ReqTask(
            uuid=f"web-{method_name.lower()}",
            adapter=cast(Any, adapter.pb_value),
            method=cast(Any, method_value),
            url=url,
            timeout_seconds=timeout,
            # 诊断路径不继承服务端的生产重试策略。不显式设的话会回落到
            # [DOWNLOADER.retry].max_attempts（默认 3），一次点击就变成 4 次完整
            # 请求——浏览器渲染下实测把一次点击拖到了 296 秒，而用户想知道的
            # 只是"这个网址现在能不能抓"，要看的是**第一次**失败的真实原因。
            max_retries=0,
            impersonate="chrome" if adapter is IPClickAdapter.CURL_CFFI else "",
        )
        body = form.get("body") or ""
        if body.strip():
            request.data = body.encode("utf-8")
        for line in (form.get("headers") or "").splitlines():
            name, sep, value = line.partition(":")
            if sep and name.strip():
                request.headers[name.strip()] = value.strip()
        return request

    # ------------------------------------------------------------------ #
    # 组件
    # ------------------------------------------------------------------ #

    def components_page(self, username: str, csrf: str) -> str:
        from ipclick.adapters.browser_settings import BrowserSettings
        from ipclick.components import COMPONENTS, snapshot
        from ipclick.web.installer import browser_body_location, detect_toolchain

        messages, errors = self._take_flash()
        toolchain = detect_toolchain()
        bodies = {c.extra: browser_body_location(c) for c in COMPONENTS if c.kind == "browser"}
        return render_components(
            snapshot(BrowserSettings.from_config(dict(self.config.get("BROWSER", {})))),
            username,
            csrf,
            toolchain=toolchain.describe() if toolchain else "既没有 pip 也没有 uv —— 无法从页面安装",
            job=self.installer.current(),
            messages=messages,
            errors=errors,
            bodies=bodies,
        )

    def component_action(self, op: str, extra: str) -> tuple[bool, str]:
        """安装 / 卸载 / 下载浏览器本体。包名走白名单，见 installer 模块。"""
        if op == "install":
            return self.installer.install(extra)
        if op == "uninstall":
            return self.installer.uninstall(extra)
        if op == "fetch":
            from ipclick.adapters.browser_settings import BrowserSettings

            kind = BrowserSettings.from_config(dict(self.config.get("BROWSER", {}))).kind
            return self.installer.fetch_browser(extra, kind)
        return False, f"未知操作 {op!r}"

    def refresh_components(self) -> tuple[bool, str]:
        """手动「刷新状态」：丢掉探测缓存，重新看磁盘。

        终端里装完/卸完之后点它，不用重启进程。
        """
        from ipclick.adapters import registry

        registry.refresh()
        return True, "已重新探测各组件的安装状态"

    def _after_install(self, job: Any) -> None:
        """安装任务结束时的回调：让新装的东西立刻可用。

        失败时也刷——可能装了一半，此时该展示的是磁盘上的真实情况。
        """
        from ipclick.adapters import registry

        registry.refresh()
        log.debug(f"依赖任务 {getattr(job, 'title', '')} 结束，已刷新组件状态与适配器注册表")

    # ------------------------------------------------------------------ #
    # 配置
    # ------------------------------------------------------------------ #

    def _groups(self) -> list[tuple[str, list[dict[str, Any]]]]:
        groups: list[tuple[str, list[dict[str, Any]]]] = []
        for title, fields in GROUPS:
            items = [
                {
                    "name": field.name,
                    "label": field.label,
                    "kind": field.kind,
                    "value": current_value(self.config, field),
                    "choices": field.choices,
                    "hint": field.hint,
                    "restart": field.restart,
                }
                for field in fields
            ]
            groups.append((title, items))
        return groups

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

    def config_page(self, username: str, csrf: str, *, generated_token: str = "") -> str:
        messages, errors = self._take_flash()
        return render_config(
            self._groups(),
            username,
            csrf,
            config_path=str(self.config_path),
            messages=messages,
            errors=errors,
            readonly_note=self._readonly(),
            generators=self._generators(),
            generated=self.take_generated(generated_token),
        )

    def generate_secret(self, env: str) -> str:
        """生成一个机密，返回取回它的 token（一次性）。

        **刻意不写进任何文件。** 机密的正规位置是 ``.env``，由人自己粘过去——
        这既是 0.3 就定下的规矩（"机密不接受从本页写入"），也顺带让"不可再次
        查看"这件事成立：服务端根本没留副本，:meth:`take_generated` 取完即弃。

        集群共享密钥尤其不能自动写：每台机器各自生成一个就全对不上了，必须在
        一台上生成再复制到其余各台，这一点在页面上会明确提示。
        """
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
        # 只记生成了什么，绝不记值本身——日志经常被收集到集中式系统里
        log.info(f"Web 端生成了新的 {spec.label}（{spec.env}），值只在页面上显示一次")
        return token

    def take_generated(self, token: str) -> dict[str, Any] | None:
        """取回刚生成的机密，**取完即弃**。"""
        if not token:
            return None
        with self._test_lock:
            return self._generated.pop(token, None)

    def save_config(self, form: dict[str, str], username: str, csrf: str) -> str:
        from ipclick.config_loader.writer import save, set_values

        updates, restart_needed, errors = parse_form(form)
        if errors:
            self._errors = errors
            return self.config_page(username, csrf)
        if not updates:
            self._errors = ["没有可保存的改动"]
            return self.config_page(username, csrf)

        try:
            text = self._read_config_text()
            new_text, changes = set_values(text, updates)
            _ = save(self.config_path, new_text)
        except (ConfigError, OSError) as e:
            self._errors = [str(e)]
            return self.config_page(username, csrf)

        self._messages = [f"已写回 {self.config_path}（{len(changes)} 项）"]
        if restart_needed:
            # 如实说：改完没反应会让人以为保存失败，然后反复点保存
            self._messages.append("这些项要重启 ipclick 才生效：" + "、".join(sorted(set(restart_needed))))
        self._apply_live(updates)
        log.info(f"Web 端保存配置：{'; '.join(changes)}")
        # 重新读一遍文件，让页面显示的是文件里真实的内容
        self._reload_config()
        return self.config_page(username, csrf)

    def _apply_live(self, updates: dict[str, dict[str, Any]]) -> None:
        """能当场生效的少数几项立刻应用，不等重启。

        只做没有副作用的：日志级别、链路记录的内存缓冲与过滤。像 worker 线程数、
        监听端口那种要重建对象的，如实标"需重启"而不是在这里偷偷重启服务。
        """
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

    # ------------------------------------------------------------------ #
    # 节点
    # ------------------------------------------------------------------ #

    def _nodes(self) -> list[dict[str, Any]]:
        from ipclick.cluster.node import ClusterConfig
        from ipclick.cluster.tokens import cluster_secret

        section = dict(self.config.get("CLUSTER", {}))
        cluster = ClusterConfig.from_config(section)
        secret = cluster_secret(section)
        return [
            {
                "id": node.id,
                "address": node.address,
                "weight": node.weight,
                "token_source": "节点列表内指定" if node.token else ("由共享密钥派生" if secret else "无（不鉴权）"),
            }
            for node in cluster.nodes
        ]

    def nodes_page(self, username: str, csrf: str) -> str:
        from ipclick.cluster.node import ClusterConfig
        from ipclick.cluster.tokens import cluster_secret

        section = dict(self.config.get("CLUSTER", {}))
        cluster = ClusterConfig.from_config(section)
        secret = cluster_secret(section)
        messages, errors = self._take_flash()
        return render_nodes(
            self._nodes(),
            username,
            csrf,
            config_path=str(self.config_path),
            self_id=self._self_id(),
            forward=cluster.forwarding_enabled,
            internal_auth=bool(secret) or any(n.token for n in cluster.nodes),
            messages=messages,
            errors=errors,
            hot_reload=self._on_cluster_changed is not None,
        )

    def probe_node(self, node_id: str, address: str = "") -> dict[str, Any]:
        """就地探一个节点：连得上吗、鉴权配对吗。

        ``address`` 允许传入表单里**还没保存**的那个地址——加完一行想先试试
        通不通是最自然的动作，非要先保存才能测就把流程割断了。
        """
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
            # "连上了但对方没设防"要单独标出来：它算通过，但绝不该被当成一切正常
            "warn": result.ok and result.remote_auth_required is False,
            "title": _probe_title(result),
            "detail": f"{result.detail}（{result.elapsed_ms} ms）",
            "elapsed_ms": result.elapsed_ms,
            "remote_version": result.remote_version,
        }

    def _self_id(self) -> str:
        service = self.task_service
        return str(getattr(service, "self_id", "") or getattr(service, "node_id", "") or "")

    def save_nodes(self, form: dict[str, str], username: str, csrf: str) -> str:
        from ipclick.config_loader.writer import save, set_nodes

        nodes = parse_nodes(form)
        errors = validate_nodes(nodes)
        if errors:
            self._errors = errors
            return self.nodes_page(username, csrf)

        # 保留原有条目里网页不写的字段（token / region / zone），否则一次保存
        # 就会把配置文件里手写的令牌抹掉。
        preserved = {n.id: n for n in self._existing_nodes()}
        for node in nodes:
            old = preserved.get(str(node["id"]))
            if old is None:
                continue
            for key in ("token", "region", "zone"):
                value = getattr(old, key, "")
                if value:
                    node[key] = value

        try:
            new_text = set_nodes(self._read_config_text(), nodes)
            _ = save(self.config_path, new_text)
        except (ConfigError, OSError) as e:
            self._errors = [str(e)]
            return self.nodes_page(username, csrf)

        self._messages = [f"已写回 {len(nodes)} 个节点到 {self.config_path}"]
        log.info(f"Web 端保存节点列表：{[n['address'] for n in nodes]}")
        self._reload_config()

        # 热更新：0.3 只写文件，真正在路由的 ClusterConfig / NodePool 在构造时就
        # 建好存死了，所以页面上只能写"改完需要重启才生效"。现在保存完立刻重建。
        if self._on_cluster_changed is not None:
            try:
                ok, message = self._on_cluster_changed()
            except Exception as e:  # pragma: no cover - 热更新失败不该让保存看起来失败
                log.exception(f"集群配置热更新失败：{e}")
                self._errors.append(f"已写回文件，但热更新失败（重启后仍会生效）：{type(e).__name__}: {e}")
            else:
                (self._messages if ok else self._errors).append(message)
        return self.nodes_page(username, csrf)

    def _existing_nodes(self) -> tuple[Any, ...]:
        from ipclick.cluster.node import ClusterConfig

        return ClusterConfig.from_config(dict(self.config.get("CLUSTER", {}))).nodes

    # ------------------------------------------------------------------ #
    # 杂项
    # ------------------------------------------------------------------ #

    def _read_config_text(self) -> str:
        """读要改的那个文件。不存在就从随包的模板起一份——否则第一次保存会
        生成一个只有一两行的配置文件，注释和其余默认值全丢了。
        """
        if self.config_path.exists():
            return self.config_path.read_text(encoding="utf-8")
        from ipclick.config_loader.loader import example_config

        log.info(f"{self.config_path} 不存在，将以默认模板为基础创建")
        return example_config()

    def _reload_config(self) -> None:
        """写完之后重新加载配置，让页面显示文件里真实的内容。

        ``load_config`` 带 lru_cache，必须先清掉——否则页面上还是旧值，
        看起来就像保存没生效。
        """
        from ipclick.config_loader.loader import load_config

        try:
            load_config.cache_clear()
            self.config = load_config(str(self.config_path) if self.config_path.exists() else None)
        except Exception as e:  # pragma: no cover - 读回失败不该让页面挂掉
            log.warning(f"重新加载配置失败：{e}")

    def _take_flash(self) -> tuple[list[str], list[str]]:
        messages, errors = self._messages, self._errors
        self._messages, self._errors = [], []
        return messages, errors

    def dashboard_extras(self) -> dict[str, Any]:
        """总览页要用的额外数据：链路统计、最近请求、组件安装状态。"""
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
            # 五个 extras 全在这里（0.3 只有四个"渲染引擎"，niquests 完全没有展示位）
            "components": snapshot(browser),
            "active_engine": active,
            "config_path": str(self.config_path),
        }


def _readable_error(error: Exception, target_node: str = "") -> str:
    """把异常变成一句人能看懂的话。

    ``grpc.RpcError`` 的 ``str()`` 是一大坨带 ``debug_error_string`` 的 repr，
    在诊断页面上尤其糟——用户点「试一试」就是为了看清楚哪里出了问题，结果拿到
    一段要自己找重点的日志。这里只留状态码和 details，并对点名场景补一句
    "该往哪儿查"。
    """
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
    """探测结果的一句话标题。三种结论要一眼分得开。"""
    if not result.reachable:
        return "连不上"
    if result.authenticated is False:
        return "鉴权不通过"
    if result.remote_auth_required is False:
        return "通过（对方未设防）"
    return "通过"


def _fragment_url(base: str, filters: dict[str, str]) -> str:
    """给实时刷新的片段拼查询串。过滤条件必须带上，否则刷新一次就把筛选冲掉了。"""
    from urllib.parse import urlencode

    query = {k: v for k, v in filters.items() if v}
    return f"{base}?{urlencode(query)}" if query else base


__all__ = ["TEST_BODY_LIMIT", "WebPages"]
