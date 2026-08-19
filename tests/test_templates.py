from __future__ import annotations

import time
from typing import Any

import pytest

from ipclick.trace import TraceRecord
from ipclick.web.templates import (
    DEFAULT_LIVE_MS,
    attr,
    dashboard_live,
    esc,
    live_label,
    render_components,
    render_config,
    render_dashboard,
    render_deploy,
    render_login,
    render_skill,
    render_test,
    render_trace,
    set_default_theme,
    trace_live,
)


CSRF = "csrf-token-value"

USER = "admin"


@pytest.fixture
def snapshot() -> dict[str, Any]:
    return {
        "server": {
            "address": "127.0.0.1:9528",
            "grpc_address": "127.0.0.1:9528",
            "grpc_port": 9528,
            "web_address": "127.0.0.1:9527",
            "web_port": 9527,
            "version": "1.0.0",
            "mode": "standalone",
            "node_id": "node-a",
            "max_workers": 100,
            "processes": 1,
            "async_mode": False,
            "default_adapter": "curl_cffi",
            "adapters": ["curl_cffi", "niquests"],
            "compression": "auto",
            "config_path": "/etc/ipclick.toml",
        },
        "trace": {
            "process": {
                "total": 12,
                "ok": 11,
                "failed": 1,
                "success_rate": 91.7,
                "avg_ms": 42.0,
                "bytes": 4096,
                "in_flight": 2,
                "peak_in_flight": 5,
                "uptime_seconds": 3600,
                "by_status": {"2xx": 11, "failed": 1},
                "by_adapter": {"curl_cffi": {"total": 12, "ok": 11, "failed": 1, "avg_ms": 42.0, "bytes": 4096}},
                "retries": {"curl_cffi:status_code": 1},
                "rejected": {"url_not_allowed": 1},
            },
            "recorder": {"node_id": "node-a", "memory_size": 500, "in_memory": 12, "source": "memory"},
        },
        "recent": [_record()],
        "components": [_component()],
        "security": {
            "tls": "未启用（明文）",
            "auth": True,
            "block_private_networks": True,
            "block_metadata_endpoints": True,
        },
        "limits": {"per_host_max_concurrent": 4, "per_host_qps": 2.5, "wait_timeout": 30.0},
        "browser": {"engine": "camoufox", "max_pages": 4, "max_pages_effective": 4, "allow_scripts": False},
        "cluster": {
            "forward": True,
            "strategy": "round_robin",
            "self_id": "node-a",
            "internal_auth": True,
            "nodes": [
                {
                    "id": "node-a",
                    "address": "127.0.0.1:9601",
                    "status": "healthy",
                    "weight": 100,
                    "drained": False,
                    "is_self": True,
                    "total_requests": 10,
                    "total_failures": 0,
                    "last_error": "",
                    "last_checked_ago": 1.0,
                }
            ],
        },
    }


def _record() -> TraceRecord:
    return TraceRecord(
        ts=time.time(),
        uuid="req-1",
        node_id="node-a",
        adapter="curl_cffi",
        method="GET",
        url="http://example.com/page",
        status_code=200,
        duration_ms=42,
        size=1024,
        attempts=1,
        forwarded=False,
        queued_ms=0,
        error="",
        stream=False,
    )


def _component() -> dict[str, Any]:
    return {
        "name": "niquests",
        "extra": "niquests",
        "kind": "http",
        "engine": "",
        "summary": "requests 的替代",
        "package": True,
        "version": "3.21.0",
        "browser": None,
        "detail": "",
        "browser_command": "",
        "install": 'pip install "ipclick[niquests]"',
        "ready": True,
    }


def test_dashboard_renders_a_full_page(snapshot: dict[str, Any]) -> None:
    html = render_dashboard(snapshot, USER, CSRF, True)

    assert html.startswith("<!doctype html>")
    assert html.rstrip().endswith("</html>")
    assert "127.0.0.1:9528" in html
    assert "node-a" in html
    assert "curl_cffi" in html
    assert CSRF in html


def test_dashboard_live_is_a_fragment(snapshot: dict[str, Any]) -> None:
    fragment = dashboard_live(snapshot)

    assert "<!doctype html>" not in fragment
    assert "91.7" in fragment or "11" in fragment


def test_dashboard_hides_actions_when_disabled(snapshot: dict[str, Any]) -> None:
    with_actions = render_dashboard(snapshot, USER, CSRF, True)
    without = render_dashboard(snapshot, USER, CSRF, False)

    assert without.count("drain") < with_actions.count("drain")


def test_login_page_shows_the_error() -> None:
    assert "密码" in render_login()
    assert "错了" in render_login("密码错了")


def test_theme_is_applied_to_the_root_element() -> None:
    set_default_theme("dark")
    try:
        assert 'data-theme="dark"' in render_login()
    finally:
        set_default_theme("light")


def test_trace_page_renders_records() -> None:
    html = render_trace([_record()], {"recorder": {"source": "memory"}}, {}, USER, CSRF)

    assert "example.com/page" in html
    assert "curl_cffi" in html


def test_trace_live_is_a_fragment() -> None:
    fragment = trace_live([_record()], {"recorder": {"source": "memory"}})

    assert "<!doctype html>" not in fragment
    assert "example.com/page" in fragment


def test_trace_page_survives_an_empty_history() -> None:
    html = render_trace([], {}, {}, USER, CSRF)
    assert "<!doctype html>" in html


def test_test_page_renders_the_form_and_result() -> None:
    choices = [{"title": "HTTP 适配器", "items": [{"value": "curl_cffi", "label": "curl_cffi", "available": True}]}]
    result = {
        "status_code": 200,
        "effective_url": "http://example.com/",
        "elapsed_ms": 42,
        "size": 1024,
        "headers": {"content-type": "text/html"},
        "trace": {"node_id": "node-a", "adapter": "curl_cffi", "attempts": 1, "forwarded": False, "queued_ms": 0},
        "body": "<h1>hi</h1>",
        "shown": 11,
        "truncated": False,
    }
    html = render_test({"url": "http://example.com/"}, result, choices, USER, CSRF)

    assert "http://example.com/" in html
    assert "node-a" in html
    assert "&lt;h1&gt;hi&lt;/h1&gt;" in html


def test_components_page_renders_cards() -> None:
    html = render_components(
        [_component()],
        USER,
        CSRF,
        toolchain="uv",
        job=None,
        messages=[],
        errors=[],
        bodies={},
    )
    assert "niquests" in html
    assert "3.21.0" in html


def test_config_page_renders_groups() -> None:
    groups = [
        (
            "服务端",
            [
                {
                    "name": "SERVER.port",
                    "label": "gRPC 端口",
                    "kind": "int",
                    "value": 9528,
                    "hint": "",
                    "restart": True,
                    "choices": [],
                }
            ],
        )
    ]
    html = render_config(
        groups,
        USER,
        CSRF,
        config_path="/etc/ipclick.toml",
        messages=["已保存"],
        errors=[],
        readonly_note=[("版本", "1.0.0")],
    )
    assert "gRPC 端口" in html
    assert "已保存" in html


def test_deploy_page_renders_the_plan() -> None:
    plan = {
        "node_id": "node-b",
        "address": "10.0.0.2:9601",
        "toml": "[SERVER]\nport = 9601\n",
        "env": "IPCLICK_AUTH_TOKEN=x",
        "commands": [{"title": "装依赖", "command": "uv sync"}],
        "web_port": 9527,
    }
    html = render_deploy(plan, USER, CSRF, total_nodes=2)

    assert "node-b" in html
    assert "uv sync" in html


def test_skill_page_renders_markdown() -> None:
    html = render_skill("# 标题\n正文", USER, CSRF, version="1.0.0", description="说明", install_dir=".claude/skills")

    assert "标题" in html
    assert ".claude/skills" in html


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("<script>", "&lt;script&gt;"), ("a&b", "a&amp;b"), ('q"q', "q&quot;q"), (7, "7")],
)
def test_escaping_covers_the_dangerous_characters(raw: Any, expected: str) -> None:
    assert esc(raw) == expected


def test_attribute_escaping_quotes_the_value() -> None:
    assert attr('a"b') == "a&quot;b"


def test_live_label_is_human_readable() -> None:
    assert "关闭" in live_label(0)
    assert "秒" in live_label(DEFAULT_LIVE_MS)
