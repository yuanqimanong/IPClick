from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, final

from ipclick.adapters import browser_engines
from ipclick.adapters.browser_settings import BrowserSettings
from ipclick.utils import module_probe


ComponentKind = Literal["http", "browser"]


@final
@dataclass(frozen=True)
class Component:
    name: str
    extra: str
    modules: tuple[str, ...]
    distribution: str
    kind: ComponentKind
    engine: str = ""
    summary: str = ""
    browser_command: str = ""


COMPONENTS: tuple[Component, ...] = (
    Component(
        name="niquests",
        extra="niquests",
        modules=("niquests",),
        distribution="niquests",
        kind="http",
        summary="requests 的 drop-in 替代，额外支持 HTTP/2 与 HTTP/3。无指纹伪装。",
    ),
    Component(
        name="camoufox",
        extra="camoufox",
        modules=("camoufox", "playwright"),
        distribution="camoufox",
        kind="browser",
        engine="camoufox",
        summary="Firefox 内核，反检测最彻底，自带指纹伪装。Linux/macOS 默认。",
        browser_command="python -m camoufox fetch",
    ),
    Component(
        name="patchright",
        extra="patchright",
        modules=("patchright",),
        distribution="patchright",
        kind="browser",
        engine="patchright",
        summary="Playwright 的反检测分支，API 完全兼容。",
        browser_command="patchright install chromium",
    ),
    Component(
        name="playwright",
        extra="playwright",
        modules=("playwright",),
        distribution="playwright",
        kind="browser",
        engine="playwright",
        summary="原版，最稳、行为最可预期，无反检测处理。",
        browser_command="playwright install chromium",
    ),
    Component(
        name="DrissionPage",
        extra="drissionpage",
        modules=("DrissionPage",),
        distribution="drissionpage",
        kind="browser",
        engine="drissionpage",
        summary="CDP 直连本机已装的 Chrome，不额外下浏览器。Windows 默认。",
    ),
)

BY_EXTRA: dict[str, Component] = {c.extra: c for c in COMPONENTS}

BY_NAME: dict[str, Component] = {c.name: c for c in COMPONENTS}

CORE_ADAPTER = "curl_cffi"

GENERIC_BROWSER = "browser"


def package_status(component: Component) -> tuple[bool, str | None]:
    installed = all(module_probe.installed(m) for m in component.modules)
    return installed, module_probe.version(component.distribution) if installed else None


def status(component: Component, browser: BrowserSettings | None = None) -> dict[str, Any]:
    installed, version = package_status(component)
    body: bool | None = None
    detail = ""
    if component.kind == "browser" and installed:
        body, detail = browser_engines.browser_ready(component.engine, browser)

    return {
        "name": component.name,
        "extra": component.extra,
        "kind": component.kind,
        "engine": component.engine,
        "summary": component.summary,
        "package": installed,
        "version": version,
        "browser": body,
        "detail": detail,
        "browser_command": component.browser_command,
        "install": f'pip install "ipclick[{component.extra}]"',
        "ready": installed and body is not False,
    }


def snapshot(browser: BrowserSettings | None = None) -> list[dict[str, Any]]:
    return [status(c, browser) for c in COMPONENTS]


def adapter_choices(browser: BrowserSettings | None = None) -> list[dict[str, Any]]:
    resolved = browser or BrowserSettings()
    enabled = resolved.enabled
    try:
        active = browser_engines.resolve_engine(resolved.engine) if enabled else ""
    except Exception:
        active = ""

    http_items: list[dict[str, Any]] = [
        {
            "value": CORE_ADAPTER,
            "label": f"{CORE_ADAPTER}（默认）",
            "available": True,
            "hint": "核心依赖，唯一带浏览器指纹伪装的适配器",
        }
    ]
    browser_items: list[dict[str, Any]] = [
        {
            "value": GENERIC_BROWSER,
            "label": f"{GENERIC_BROWSER}（自动选择引擎）",
            "available": enabled,
            "hint": (
                f"由服务端按 [BROWSER].engine 决定，当前解析为 {active or '—'}"
                if enabled
                else "浏览器渲染已关闭（[BROWSER].enabled = false）"
            ),
        }
    ]

    for component in COMPONENTS:
        state = status(component, resolved)
        target = http_items if component.kind == "http" else browser_items
        if component.kind == "browser" and not enabled:
            hint = "浏览器渲染已关闭（[BROWSER].enabled = false）"
            available = False
        elif not state["package"]:
            hint = state["install"]
            available = False
        elif state["browser"] is False:
            hint = f"浏览器本体未就绪：{component.browser_command}"
            available = False
        else:
            hint = component.summary
            available = True
        target.append(
            {
                "value": component.name,
                "label": component.name,
                "available": available,
                "hint": hint,
            }
        )

    return [
        {"title": "HTTP 适配器", "items": http_items},
        {"title": "浏览器渲染", "items": browser_items},
    ]


__all__ = [
    "BY_EXTRA",
    "BY_NAME",
    "COMPONENTS",
    "CORE_ADAPTER",
    "GENERIC_BROWSER",
    "Component",
    "ComponentKind",
    "adapter_choices",
    "package_status",
    "snapshot",
    "status",
]
