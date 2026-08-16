"""可选组件清单：五个 extras 的元信息、安装状态与分类。

这份清单是**唯一**的事实来源。此前"IPClick 到底支持哪些可选组件"散在四个地方
（``pyproject.toml`` 的 extras、``browser_engines.INSTALL_HINTS``、注册表的 if、
Web 端那张只有渲染引擎的表），于是：

* niquests 是纯 HTTP 适配器，不属于"渲染引擎"，Web 端**完全没有**它的位置；
* 没装的组件直接从「试一试」的下拉框里消失，对着文档看的人会觉得实现对不上；
* 通用占位值 ``browser`` 和真实适配器名混在同一层级，看起来像第六个 extra。

现在三处都从这里生成：状态展示、安装/卸载、下拉框分组。

两级安装状态
------------
**Python 包**和**浏览器本体**是两件事，必须分开报——只报一个的话，
``pip install "ipclick[camoufox]"`` 但没 fetch 的机器会显示"已安装"，而第一次
请求会卡几分钟去下 1 GB 然后超时。
"""

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
    """一个可选组件（= ``pyproject.toml`` 里的一个 extra）。"""

    #: 适配器名 / 引擎名。这也是「试一试」下拉框里的取值。
    name: str
    #: ``pip install "ipclick[<extra>]"`` 里的那个 extra
    extra: str
    #: 探测用的**顶层**模块名。可能有多个（camoufox 还要 playwright 才算装上）。
    modules: tuple[str, ...]
    #: PyPI 发行版名，用于查版本号。注意它和模块名经常对不上
    #: （发行版 ``drissionpage`` vs 模块 ``DrissionPage``）。
    distribution: str
    kind: ComponentKind
    #: 浏览器引擎名。HTTP 适配器为空串。
    engine: str = ""
    #: 一句话说明它的价值——用户要在五个里选，得知道差别在哪。
    summary: str = ""
    #: 浏览器本体的准备命令。用系统浏览器的引擎（drissionpage）为空串。
    browser_command: str = ""


#: 全部可选组件。顺序即展示顺序。
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

#: 按 extra 索引。安装/卸载的白名单就是它的键集合——**绝不**拼接用户输入。
BY_EXTRA: dict[str, Component] = {c.extra: c for c in COMPONENTS}

#: 按组件名索引
BY_NAME: dict[str, Component] = {c.name: c for c in COMPONENTS}

#: 核心适配器：随 ``pip install ipclick`` 一起装，卸不掉也不用装。
CORE_ADAPTER = "curl_cffi"

#: 通用占位值："用浏览器渲染就行，具体引擎由服务端按 [BROWSER].engine 决定"。
#: **不是** extra —— 它在下拉框里必须和真实组件名区分开，否则会被当成第六个组件。
GENERIC_BROWSER = "browser"


def package_status(component: Component) -> tuple[bool, str | None]:
    """``(Python 包装了没, 版本号)``。走 find_spec，因此卸载也能如实反映。"""
    installed = all(module_probe.installed(m) for m in component.modules)
    return installed, module_probe.version(component.distribution) if installed else None


def status(component: Component, browser: BrowserSettings | None = None) -> dict[str, Any]:
    """一个组件的完整状态，给 Web 端直接用。"""
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
        # None = 查不出来。宁可显示"未知"，也不要把一台装好的机器误报成没装。
        "browser": body,
        "detail": detail,
        "browser_command": component.browser_command,
        "install": f'pip install "ipclick[{component.extra}]"',
        # 能不能直接拿来用：包在、且本体不是明确的"没有"
        "ready": installed and body is not False,
    }


def snapshot(browser: BrowserSettings | None = None) -> list[dict[str, Any]]:
    """全部组件的状态，按 :data:`COMPONENTS` 的顺序。"""
    return [status(c, browser) for c in COMPONENTS]


def adapter_choices(browser: BrowserSettings | None = None) -> list[dict[str, Any]]:
    """「试一试」下拉框的分组数据。

    两条和 0.3 不同的规则：

    * **没装的也要列出来**，置灰并附上安装命令，而不是从列表里消失。消失会让
      对着文档看的人以为文档和实现对不上，也不知道 IPClick 到底支持哪些。
    * ``browser`` 单独成项并写明"引擎由服务端自动选"，不和真实组件名混排。
    """
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
            # 总开关关掉时它一定失败，置灰比让人试一次再看报错强
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
            # 包装了但本体没下：这是最容易被误判成"能用"的状态，必须单独说
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
