from __future__ import annotations

from collections import OrderedDict
import json as json_lib
import secrets
import threading
from typing import Any, cast, final

from ipclick.dto.models import METHOD_MAP, IPClickAdapter
from ipclick.dto.proto import task_pb2
from ipclick.exceptions import ValidationError
from ipclick.services.detached import DetachedContext
from ipclick.utils.config_util import section
from ipclick.utils.log_util import log
from ipclick.web.pages.context import PageContext
from ipclick.web.templates import render_test


TEST_BODY_LIMIT = 256 * 1024

TEST_TIMEOUT_MAX = 120.0

TEST_RETRIES_MAX = 5

TEST_RESULT_KEEP = 20

CURL_MAX_LEN = 64 * 1024


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


@final
class SandboxPage:
    def __init__(self, ctx: PageContext) -> None:
        self.ctx: PageContext = ctx
        self._results: OrderedDict[str, tuple[dict[str, str], dict[str, Any]]] = OrderedDict()
        self._lock: threading.Lock = threading.Lock()

    def _adapter_choices(self) -> list[dict[str, Any]]:
        from ipclick.adapters.browser_settings import BrowserSettings
        from ipclick.components import adapter_choices

        return adapter_choices(BrowserSettings.from_config(section(self.ctx.config, "BROWSER")))

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
            nodes=self.ctx.target_nodes(),
            curl_notes=curl_notes,
            curl_error=curl_error,
            allow_scripts=BrowserSettings.from_config(section(self.ctx.config, "BROWSER")).allow_scripts,
        )

    def import_curl(self, form: dict[str, str]) -> tuple[dict[str, str], list[str], str]:
        from ipclick.web.curl_parser import parse_curl

        raw = (form.get("curl") or "")[:CURL_MAX_LEN]
        parsed = parse_curl(raw)
        if not parsed.ok:
            return {}, parsed.notes, parsed.error or "解析失败"
        return parsed.as_form(), parsed.notes, ""

    def stash_test_result(self, form: dict[str, str], result: dict[str, Any]) -> str:
        token = secrets.token_urlsafe(9)
        with self._lock:
            self._results[token] = (dict(form), result)
            while len(self._results) > TEST_RESULT_KEEP:
                _ = self._results.popitem(last=False)
        return token

    def take_test_result(self, token: str) -> tuple[dict[str, str], dict[str, Any] | None]:
        if not token:
            return {}, None
        with self._lock:
            entry = self._results.get(token)
        return entry if entry is not None else ({}, None)

    def run_test(self, form: dict[str, str]) -> dict[str, Any]:
        if self.ctx.task_service is None:
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
                sender = getattr(self.ctx.task_service, "send_from_thread", None)
                if callable(sender):
                    response = cast(
                        "task_pb2.TaskResp", sender(request, cast(Any, DetachedContext().as_servicer_context()))
                    )
                else:
                    response = self.ctx.task_service.Send(request, cast(Any, DetachedContext().as_servicer_context()))
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
        service = self.ctx.task_service
        sender = getattr(service, "send_to_node", None)
        if callable(sender):
            return cast(task_pb2.TaskResp, sender(request, node_id))
        return self._direct_send(request, node_id)

    def _direct_send(self, request: task_pb2.ReqTask, node_id: str) -> task_pb2.TaskResp:
        deadline = request.timeout_seconds * (request.max_retries + 1) + 15
        return cast(
            task_pb2.TaskResp,
            self.ctx.call_node(
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

        resolved = ProxyConfig(**proxy_config(self.ctx.config)).to_url()
        if not resolved:
            raise ValidationError("选了「用配置文件里的 [PROXY]」，但那一节没有配 host / tunnel_server")
        return resolved
