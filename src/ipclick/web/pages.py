"""请求流 / 试一试 / 组件 / 配置 / AI 接入 这几页的业务逻辑。

从 :mod:`ipclick.web.server` 拆出来：那个模块负责 HTTP（路由、会话、CSRF、
响应头），这个负责"页面要展示什么、提交上来要怎么处理"。混在一起的话，
每加一页都得往 HTTP 处理器里塞一段业务代码，很快就没法看了。

这一层刻意只依赖注入进来的对象（TaskService、记录器、配置），不自己去
import 服务端——这样它在测试里可以单独构造。
"""

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


#: "试一试"页面最多显示多少字节源码。再多的话浏览器渲染会卡，
#: 而看源码这件事看前几十 KB 基本就够判断了。
TEST_BODY_LIMIT = 256 * 1024

#: "试一试"页面允许的超时上限（秒）。页面是同步等结果的，
#: 让它能等十分钟等于给自己留一个占满 worker 的口子。
TEST_TIMEOUT_MAX = 120.0

#: "试一试"页面允许的重试次数上限。同一个理由：一次点击最多占用
#: ``TEST_TIMEOUT_MAX × (上限 + 1)`` 秒，再高就该去命令行发了。
#: 页面上的提示文案是 :data:`ipclick.web.templates.TEST_RETRIES_MAX_HINT`，
#: 两者由 tests/test_web_pages.py 盯着不许失步。
TEST_RETRIES_MAX = 5

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

#: 远程组件操作的 RPC 超时（秒）。
#:
#: 只覆盖"把任务交出去"这一下，不是等它装完——装是异步的，主控随后轮询。
#: 给得比普通探测宽是因为对端可能正忙（它是个在干活的节点），但也不能没有上限。
REMOTE_COMPONENT_TIMEOUT = 15.0

#: 「添加节点」时预填端口的起点。
#:
#: 刻意和主控自己那两个默认端口（9527 Web / 9528 gRPC）分开一个号段：混在一起时，
#: 人看到 9529 分不清"这是谁的"。从 19001 起数，一眼就知道是子节点。
NODE_PORT_BASE = 19001


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
        cli_port: int | None = None,
        runtime_ports: dict[str, int] | None = None,
    ):
        self.config: Settings = config
        #: 进程**实际**在听的端口，键为配置项名（``SERVER.port`` / ``WEB.port``）。
        #:
        #: 配置页读的是**文件**（见 :func:`ipclick.web.editable.current_value`），那是
        #: 它的正确职责——那一格是"要写回文件的值"。但 ``ipclick run --port X`` 不改
        #: 文件，于是页面显示 9528、进程实际在 X 上，而页面对此一个字都不说。
        #: 这里存的是第二个数字，只用于**显示**，不参与写回。
        self.runtime_ports: dict[str, int] = dict(runtime_ports or {})
        self.recorder: TraceRecorder = recorder
        self.task_service: TaskService | None = task_service
        self._cluster_snapshot: Any = cluster_snapshot
        #: 保存节点之后调一次，让改动**立即生效**而不是等重启。由服务端注入
        #: （它才有权重建转发路由与观测池）。
        self._on_cluster_changed: Callable[[], tuple[bool, str]] | None = on_cluster_changed
        from ipclick.config_loader.writer import target_path

        # 和进程实际读的那个文件保持一致：带 --port 起的实例读的是
        # ipclick-<端口>.toml，页面要是写回 ipclick.toml 就成了"改了没反应"。
        self.config_path: Path = target_path(config_path, cli_port)
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
        """能点名的节点。

        0.4 里这份列表只在**开了服务端转发**时才非空——理由是"没开转发的话本进程
        不转发，选了也只会打到自己身上"。那个理由只对"走 TaskService"这一条路成立。
        配了节点却看不到下拉框的人并不知道这层区别，只会觉得功能没做。

        0.5 改成：配置里有节点就列出来。开了转发走转发器的 ``send_to_node``；
        没开就由这一页**直连**那台机器发一次 gRPC（见 :meth:`_direct_send`）——
        目的本来就是"验证我刚加的这台配对没有"，和本机转不转发无关。
        """
        service = self.task_service
        cluster = getattr(service, "cluster", None) if service is not None else None
        nodes = list(getattr(cluster, "nodes", ()) or [])
        if not nodes:
            # 没开转发时本进程没有 cluster 对象，直接读配置
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
        """点名发给某个节点。开了转发走转发器，没开就直连。"""
        service = self.task_service
        sender = getattr(service, "send_to_node", None)
        if callable(sender):
            return cast(task_pb2.TaskResp, sender(request, node_id))
        return self._direct_send(request, node_id)

    def _direct_send(self, request: task_pb2.ReqTask, node_id: str) -> task_pb2.TaskResp:
        """不经本进程的路由，直接对那台机器发一次 gRPC。

        连法（令牌派生、TLS、开关 channel）走 :meth:`_call_node`——和远程组件管理
        共用同一套，两处各写一份的话迟早出现"试一试能连、装组件连不上"这种自相
        矛盾的状态。
        """
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
        """把表单组装成 ReqTask。

        字段与 :meth:`ipclick.sdk.Downloader.request` **一一对应**——页面上能调的
        参数少于 SDK 的话，"试一试"就验不出真实调用会怎样，而那正是这一页存在的
        全部理由。刻意不做的只有 ``stream``：这一页同步等结果再整页渲染，流式在
        这里没有任何可观察的差别，加个开关只会让人以为验证过了。
        """
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

        # 重试次数默认 0 —— 诊断路径不继承服务端的生产重试策略。
        # 不显式设的话会回落到 [DOWNLOADER.retry].max_attempts（默认 3），一次点击
        # 就变成 4 次完整请求：浏览器渲染下实测把一次点击拖到 296 秒，而用户想知道
        # 的只是"这个网址现在能不能抓"，要看的是**第一次**失败的真实原因。
        # 现在这一项摆到了页面上，想验重试行为的人可以自己调高。
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

        # 指纹伪装只对 curl_cffi 有意义；留空时服务端按 "chrome" 处理
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

        # 浏览器渲染专属。automation_script 还要服务端开了 [BROWSER].allow_scripts
        # 才会执行——这里照发，让服务端给出那条明确的拒绝理由，而不是页面上先拦一道
        # 说法不同的。
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
        """请求体。``data`` 与 ``json`` 互斥，所以用一个单选决定这段文本是哪一种。"""
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
        """请求头 / Cookie / 查询参数。三者都是"每行一条"的文本框。"""
        for line in (form.get("headers") or "").splitlines():
            name, sep, value = line.partition(":")
            if sep and name.strip():
                request.headers[name.strip()] = value.strip()
        for line in (form.get("cookies") or "").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip():
                request.cookies[name.strip()] = value.strip()
        # params 在协议里是一个字符串字段（服务端按 JSON 解析），不是 map
        pairs: dict[str, str] = {}
        for line in (form.get("params") or "").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip():
                pairs[name.strip()] = value.strip()
        if pairs:
            request.params = json_lib.dumps(pairs, ensure_ascii=False)

    def _resolve_proxy(self, form: dict[str, str]) -> str:
        """代理。三档：不用 / 用配置里的 [PROXY] / 自定义 URL。

        选"用配置"时在这里就解析成 URL，页面才能在结果里显示"这次到底走了哪个代理"
        （只显示 host:port，账号密码不回显）。
        """
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

    # ------------------------------------------------------------------ #
    # 组件
    # ------------------------------------------------------------------ #

    def components_page(self, username: str, csrf: str, *, node_id: str = "") -> str:
        """组件页。``node_id`` 非空时展示的是**那台子节点**的情况。

        集群里每台机器都要各自装一遍适配器，逐台 SSH 上去敲命令是部署时最烦的
        一步。所以这一页可以点名一台机器——和「试一试」同一个心智模型。
        """
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
                # 对端的工具链我们看不到（那要再加一个字段），而这一行的用途是
                # "确认装到哪个环境去了"——远程时那是对端的事，如实说不知道。
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

    # ------------------------------------------------------------------ #
    # AI 接入
    # ------------------------------------------------------------------ #

    def skill_markdown(self) -> str:
        """技能正文。``/skill.md`` 直接吐它，页面里也嵌同一份。"""
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
        """在**某台子节点**上装 / 卸组件，或查它装了什么。

        集群里每台机器都要各自装一遍适配器，而"逐台 SSH 上去敲命令"是这套东西
        部署时最烦的一步。所以主控的组件页可以点名一台机器。

        连法和「试一试」的点名直连是同一套（同样的集群内部令牌、同样的 TLS 设置），
        所以"探测通得过但装不了"这种自相矛盾的结果不会出现。

        对端**默认不允许**这么做，要它自己打开 ``[CLUSTER].allow_remote_install``。
        关着时会收到 PERMISSION_DENIED，这里把那句说明原样透出——那是可执行的指引，
        换成笼统的"失败"等于把答案藏起来。
        """
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
        except Exception as e:  # pragma: no cover - 诊断入口不该抛
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
        """对某个节点开一条 gRPC，跑一次 ``call``，然后关掉。

        令牌与 TLS 的解析和 :meth:`_direct_send` 是同一套——两处各写一份的话，
        迟早出现"试一试能连、装组件连不上"这种自相矛盾的状态。
        """
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
        """安装 / 卸载 / 下载浏览器本体。包名走白名单，见 installer 模块。

        ``node_id`` 非空时转成一次远程调用——白名单、命令规划、"一次只跑一个任务"
        的约束都在**对端**同样生效（对端跑的是同一份 InstallManager）。
        """
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
        """轮询用：当前（或最近一次）任务的快照。本机或某台子节点。"""
        if not node_id:
            return self.installer.current()
        return self.remote_component(node_id, "status").get("job")

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
        """这一项的实际运行值和文件里写的不一样时返回实际值，否则 0。

        只有端口有这个问题，而且只在 ``--port`` / ``--web-port`` 覆盖时出现。
        返回 0 而不是 None，是为了让模板那边一个 falsy 判断就够。
        """
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
        """「添加节点」预填哪个端口。

        从 :data:`NODE_PORT_BASE` 往上找第一个没被用过的。用一个和默认端口
        （9527/9528）明显分开的号段是有意的：这些是**子节点**的端口，和主控自己
        那两个混在一起时，人一眼看不出"这个 9529 到底是谁的"。

        找不到就回落到基数——那意味着已经加了几万台，此时预填什么都不重要了。
        """
        used = {int(port) for node in self._nodes() if (port := node["address"].rpartition(":")[2]).isdigit()}
        for candidate in range(NODE_PORT_BASE, NODE_PORT_BASE + 10000):
            if candidate not in used:
                return candidate
        return NODE_PORT_BASE

    def _cluster_tab_data(self) -> dict[str, Any]:
        """「集群设置」那一页要的东西。

        机密只报"有没有"——和页面上其余地方同一条规矩。真正的值只在部署材料页
        出现（那是它的用途），且不写日志、不落盘。
        """
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

    # ------------------------------------------------------------------ #
    # 子节点部署材料
    # ------------------------------------------------------------------ #

    def _deploy_plans(self) -> list[Any]:
        """每台节点的部署材料。

        令牌取的是**主控当前生效**的那份，所以复制过去必然对得上——集群最常见的
        故障"两边密钥差一个字符"在这里就没有发生的机会。
        """
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
        """保存配置。两个分页共用这一条路径——白名单还是那一份。

        「集群设置」页额外带两样东西：转发开关（一个复选框，映射到
        ``[CLUSTER].forward`` 的 on/off 字符串）和节点卡片里的地址 / 权重。
        它们和普通字段一起提交，因为人的心智模型是"这一页改完点一次保存"，
        而不是"配置一个保存按钮、节点另一个"。
        """
        from ipclick.config_loader.writer import save, set_nodes, set_values

        tab = form.get("tab", "basic")
        updates, restart_needed, errors = parse_form(form)
        # 只保留**真的变了**的项。
        #
        # 表单一次会把整页的字段都提交上来，而 parse_form 不知道旧值是什么，
        # 于是每次保存都报"7 项已写回、这 7 项需要重启"——改一个日志级别却被告知
        # 要重启，人只会开始无视这句提示，而它在真需要重启时是唯一的信号。
        updates, restart_needed = self._changed_only(updates, restart_needed)

        # 转发开关。复选框天然只有"勾了/没勾"，而配置里是 "on"/"off" 字符串，
        # 所以单独映射一次而不是硬塞进 Field 的 bool 类型——后者会把
        # forward = true 写进 toml，那个值 ClusterConfig 不认。
        if "__present__CLUSTER.forward_on" in form:
            updates.setdefault("CLUSTER", {})["forward"] = "on" if "CLUSTER.forward_on" in form else "off"
            restart_needed.append("服务端转发")

        # 表单里出现过任何一个 node_address_N，就说明这次提交带着节点网格——
        # 哪怕解析出来是空列表。这两件事必须分开：清空最后一台机器的地址是
        # "把它删掉"，而不是"这次没改节点"，后者会让最后一台永远删不掉。
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
            # 如实说：改完没反应会让人以为保存失败，然后反复点保存
            self._messages.append("这些项要重启 ipclick 才生效：" + "、".join(sorted(set(restart_needed))))
        self._apply_live(updates)
        log.info(f"Web 端保存配置：{'; '.join(changes) or '（仅节点）'}")
        # 重新读一遍文件，让页面显示的是文件里真实的内容
        self._reload_config()
        if tab == "cluster":
            self._hot_reload_cluster()
        return self.config_page(username, csrf, tab=tab)

    def add_node(self, form: dict[str, str], username: str, csrf: str) -> str:
        """「添加节点」弹窗的提交。

        只要 IP 和端口两项必填，其余给预置默认值——加机器是集群的日常操作，
        每次都让人把 id、权重想一遍纯属拖慢。id 留空就用 ``host:port``。
        """
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
        """滤掉和当前值相同的项，并据此重算"哪些需要重启"。"""
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
        # 转发开关不在 FIELDS 里（它是个映射出来的合成项），照原样保留
        for label in restart_needed:
            if label == "服务端转发" and "CLUSTER" in changed and "forward" in changed["CLUSTER"]:
                labels.append(label)
        return changed, labels

    def remove_node(self, form: dict[str, str], username: str, csrf: str) -> str:
        """删掉一台节点。

        走**独立**的表单，不跟「保存」共用：共用的话，点一次删除会把页面上其余
        未提交的改动（改了一半的地址、动过的转发开关）一起写进去，而人只点了删除。

        只改本机的节点列表，不碰那台机器本身——那需要远程操作它，而这里没有也不该有
        那个能力。
        """
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
        """保留原有条目里网页不写的字段（token / region / zone）。

        不保留的话，一次保存就会把配置文件里手写的令牌抹掉——而那种丢失只在下次
        转发到那台机器时才暴露成 UNAUTHENTICATED。
        """
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
        """节点改完立刻重建路由，不用重启。

        0.3 只写文件，真正在路由的 ClusterConfig / NodePool 在构造时就建好存死了，
        所以页面上只能写"改完需要重启才生效"。
        """
        if self._on_cluster_changed is None:
            return
        try:
            ok, message = self._on_cluster_changed()
        except Exception as e:  # pragma: no cover - 热更新失败不该让保存看起来失败
            log.exception(f"集群配置热更新失败：{e}")
            self._errors.append(f"已写回文件，但热更新失败（重启后仍会生效）：{type(e).__name__}: {e}")
        else:
            (self._messages if ok else self._errors).append(message)

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


def _job_from_pb(job: Any) -> dict[str, Any]:
    """把 protobuf 的任务快照转回页面用的那个 dict。

    形状必须和本机 :meth:`ipclick.web.installer.Job.snapshot` **完全一致**——
    前端那段渲染进度条的 JS 是同一份，两边差一个字段名就会在远程那条路上静默失效。
    """
    percent = job.percent
    return {
        "id": job.id,
        "title": job.title,
        "command": job.command,
        "status": job.status,
        "returncode": job.returncode,
        "elapsed": job.elapsed_seconds,
        "progress": {
            # 服务端用 -1 表达"量不出来"（proto3 的 double 没有 null）
            "percent": None if percent < 0 else round(percent, 1),
            "done_bytes": job.done_bytes,
            "speed": round(job.speed_bytes),
            "phase": job.phase,
        },
        "output": list(job.output),
    }


def _readable_component_error(error: Any, node_id: str) -> str:
    """远程组件操作失败时给一句能照着做的话。"""
    import grpc

    code = getattr(error, "code", lambda: None)()
    details = (getattr(error, "details", lambda: "")() or "").strip()

    if code is grpc.StatusCode.PERMISSION_DENIED:
        # 对端已经把该说的说清楚了（要改哪一项、为什么默认关），原样透出
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
    """配置里的旧值和表单提交的新值算不算"没变"。

    数字要跨类型比：toml 里写 ``60`` 读出来是 int，而表单上的"超时（秒）"是
    float 字段，解析出来是 ``60.0``——按 ``==`` 比对本来也相等，但 bool 是 int
    的子类，``True == 1`` 会把"开关没动"和"值从 1 改成 True"混为一谈，所以
    bool 单独走一条。
    """
    if isinstance(current, bool) or isinstance(new, bool):
        return isinstance(current, bool) and isinstance(new, bool) and current == new
    if isinstance(current, (int, float)) and isinstance(new, (int, float)):
        return float(current) == float(new)
    return current == new


def _as_number(raw: str | None, default: float, label: str) -> float:
    """表单里的数字。留空取默认值，写错了报出**哪一项**写错了。

    统一报错文案是有意的：三四个数字输入框各写一套"必须是数字"，措辞迟早会散。
    """
    text = (raw or "").strip()
    if not text:
        return default
    try:
        return float(text)
    except ValueError as e:
        raise ValidationError(f"{label}必须是数字，收到 {text!r}") from e


def _parse_status_codes(raw: str | None) -> list[int]:
    """``200, 404`` 这样的一行文本 -> ``[200, 404]``。

    这一项是"哪些状态码不算失败、不触发重试"。写错一个字符就整项丢掉的话，
    用户会以为自己设了而实际没设，所以非法值直接报错。
    """
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


def _live_ms(query: dict[str, str]) -> int:
    """请求流的刷新间隔（毫秒），0 表示关掉。

    没提交过表单（查询串里没有 ``_`` 标记）就给默认档——这一页存在的意义就是
    看着请求打进来，默认关掉等于默认没用。

    ``live=1`` 是 0.4 那个复选框的取值。老书签、老链接点开时把它映射到默认档，
    而不是让它落到"取值不认识"的分支上变成关闭。

    **只有空串才是"关闭"**（0.4 那个复选框不勾选时提交的就是空串）。其余认不出来的
    取值——超出档位表的数字、被截断的链接、乱码——一律回落到默认档，而不是关闭：
    地址栏被改坏时该退回"能用"，把这一页唯一的功能悄悄关掉才是最难查的那种。
    """
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
    """给实时刷新的片段拼查询串。过滤条件必须带上，否则刷新一次就把筛选冲掉了。"""
    from urllib.parse import urlencode

    query = {k: v for k, v in filters.items() if v}
    return f"{base}?{urlencode(query)}" if query else base


__all__ = ["TEST_BODY_LIMIT", "WebPages"]
