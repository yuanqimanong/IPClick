"""请求流 / 试一试 / 配置 / 节点 这几页的业务逻辑。

从 :mod:`ipclick.web.server` 拆出来：那个模块负责 HTTP（路由、会话、CSRF、
响应头），这个负责"页面要展示什么、提交上来要怎么处理"。混在一起的话，
每加一页都得往 HTTP 处理器里塞一段业务代码，很快就没法看了。

这一层刻意只依赖注入进来的对象（TaskService、记录器、配置），不自己去
import 服务端——这样它在测试里可以单独构造。
"""

from __future__ import annotations

from collections import OrderedDict
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
from ipclick.web.templates import render_config, render_nodes, render_test, render_trace


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
    ):
        self.config: Settings = config
        self.recorder: TraceRecorder = recorder
        self.task_service: TaskService | None = task_service
        self._cluster_snapshot: Any = cluster_snapshot
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
        )

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

    def _adapters(self) -> list[str]:
        """「试一试」下拉框里能选什么。

        注册表里只有**本机装得上**的适配器，所以这个列表天然不会让人选到一个
        缺依赖的东西。额外补一个 ``browser``——那是"用浏览器渲染，具体引擎由
        服务端决定"的通用请求方式，它不在注册表里（请求时才解析成具体引擎），
        但恰恰是调用方最常用的写法。
        """
        from ipclick.adapters.browser_settings import BrowserSettings
        from ipclick.adapters.registry import ADAPTER_CLASSES

        names = sorted(ADAPTER_CLASSES)
        if BrowserSettings.from_config(dict(self.config.get("BROWSER", {}))).enabled:
            names.append("browser")
        return names

    def test_page(self, form: dict[str, str], result: dict[str, Any] | None, username: str, csrf: str) -> str:
        return render_test(form, result, self._adapters(), username, csrf)

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

        context = _WebContext()
        try:
            response = self.task_service.Send(request, cast(Any, context))
        except Exception as e:  # 页面不该因为一次试探而 500
            log.exception(f"试一试请求失败：{e}")
            return {"error_only": True, "error": f"{type(e).__name__}: {e}"}

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

    def config_page(self, username: str, csrf: str) -> str:
        messages, errors = self._take_flash()
        return render_config(
            self._groups(),
            username,
            csrf,
            config_path=str(self.config_path),
            messages=messages,
            errors=errors,
            readonly_note=self._readonly(),
        )

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
        )

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

        self._messages = [f"已写回 {len(nodes)} 个节点到 {self.config_path}，重启后生效"]
        log.info(f"Web 端保存节点列表：{[n['address'] for n in nodes]}")
        self._reload_config()
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
        """总览页要用的额外数据：链路统计、最近请求、引擎安装状态。"""
        from ipclick.adapters.browser_engines import ENGINE_NAMES, INSTALL_HINTS, engine_status, resolve_engine
        from ipclick.adapters.browser_settings import BrowserSettings

        browser = BrowserSettings.from_config(dict(self.config.get("BROWSER", {})))
        engines: list[dict[str, Any]] = []
        if browser.enabled:
            # 分两级报："包装了没"和"浏览器本体下了没"。只报一个的话，
            # pip 装完但没 fetch 的机器会显示"已安装"，而第一次用会卡几分钟
            engines = [
                {
                    "name": name,
                    "package": status.package,
                    "browser": status.browser,
                    "label": status.label,
                    "detail": status.detail,
                    "available": status.ready,
                    "install": INSTALL_HINTS.get(name, ""),
                }
                for name, status in ((n, engine_status(n, browser)) for n in sorted(ENGINE_NAMES))
            ]
        try:
            active = resolve_engine(browser.engine) if browser.enabled else ""
        except Exception:
            active = browser.engine
        return {
            "trace": self.recorder.stats(),
            "recent": self.recorder.recent(limit=12),
            "engines": engines,
            "active_engine": active,
            "config_path": str(self.config_path),
        }


__all__ = ["TEST_BODY_LIMIT", "WebPages"]
