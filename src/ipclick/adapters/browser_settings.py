"""浏览器渲染参数。

把配置文件的 ``[BROWSER]`` 节变成浏览器适配器真正会读的对象。

原来那一节写了一堆配置——插件目录、``.crx``/``.dll`` 列表、缓存上限 MB 数、
``sandbox.level = strict/moderate/off``——没有任何一项有消费方，也没有任何一项
能落到 playwright 上。这里只保留能真正生效的键，其余全部删掉：和
``[DOWNLOADER]`` 一样的原则，宁可少配也不要"配了不生效"。
"""

from dataclasses import dataclass, field
from typing import Any


#: 可选的浏览器内核
BROWSER_KINDS: frozenset[str] = frozenset({"chromium", "firefox", "webkit"})

#: page.goto 的等待时机，取值来自 playwright
WAIT_UNTIL_CHOICES: frozenset[str] = frozenset({"load", "domcontentloaded", "networkidle", "commit"})

#: 可被拦截的资源类型（playwright 的 request.resource_type 取值）
BLOCKABLE_RESOURCES: frozenset[str] = frozenset(
    {"image", "media", "font", "stylesheet", "script", "xhr", "fetch", "websocket", "other"}
)


def _as_float(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result > 0 else default


def _as_int(value: Any, default: int, *, minimum: int = 1) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result if result >= minimum else default


def _as_str_tuple(value: Any, allowed: frozenset[str] | None = None) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    items = (str(v).strip().lower() for v in value)
    return tuple(v for v in items if v and (allowed is None or v in allowed))


@dataclass(frozen=True)
class BrowserSettings:
    """浏览器渲染的默认行为。请求级 ``automation_config`` 优先于这里的值。"""

    enabled: bool = True

    # 内核与启动
    kind: str = "chromium"
    headless: bool = True
    #: 系统浏览器路径。留空则用 playwright 自己下载的那份
    #: （``playwright install chromium``，约 150MB）。容器里通常指向系统 chromium。
    executable_path: str | None = None
    #: 传给浏览器进程的额外命令行参数
    args: tuple[str, ...] = ()
    #: 容器里没有 user namespace 时 chromium 起不来，需要 --no-sandbox。
    #: 默认 False——关沙箱会让页面里的代码更容易逃逸到宿主进程，得由部署方明确选择。
    no_sandbox: bool = False

    # 页面
    user_agent: str | None = None
    viewport_width: int = 1920
    viewport_height: int = 1080

    # 超时（秒）
    page_load_timeout: float = 30.0
    script_timeout: float = 60.0

    #: 默认的 page.goto 等待时机
    wait_until: str = "load"

    #: 默认拦截的资源类型。图片 / 字体 / 视频对取 HTML 没用，拦掉能省大量时间和带宽。
    block_resources: tuple[str, ...] = ("image", "media", "font")

    #: 同时打开的页面上限。无头浏览器每个页面几十上百 MB，不设上限很容易把机器打满。
    max_pages: int = 4

    #: 是否允许请求携带 automation_script（在页面里执行任意 JS）。
    #: 默认关闭：页面 JS 能绕过服务端的 URL 策略去访问内网，等于把 SSRF 防线
    #: 整个让开。需要时由部署方显式打开，并自行确保只有可信调用方能访问。
    allow_scripts: bool = False

    # 代理
    proxy_gateway: str | None = None
    proxy_bypass: tuple[str, ...] = field(default_factory=tuple)

    @property
    def viewport(self) -> dict[str, int]:
        return {"width": self.viewport_width, "height": self.viewport_height}

    @classmethod
    def from_config(cls, browser_config: dict[str, Any] | None) -> "BrowserSettings":
        """从配置文件的 ``[BROWSER]`` 节构造。缺失或非法的项回落到默认值。"""
        config = dict(browser_config or {})
        timeout = dict(config.get("timeout") or {})
        proxy = dict(config.get("proxy") or {})
        viewport = dict(config.get("viewport") or {})

        defaults = cls()

        kind = str(config.get("browser", defaults.kind)).strip().lower()
        if kind not in BROWSER_KINDS:
            kind = defaults.kind

        wait_until = str(config.get("wait_until", defaults.wait_until)).strip().lower()
        if wait_until not in WAIT_UNTIL_CHOICES:
            wait_until = defaults.wait_until

        executable = str(config.get("executable_path") or "").strip() or None
        gateway = str(proxy.get("gateway") or "").strip() or None
        user_agent = str(config.get("user_agent") or "").strip() or None

        blocked = config.get("block_resources")
        block_resources = (
            _as_str_tuple(blocked, BLOCKABLE_RESOURCES) if blocked is not None else defaults.block_resources
        )

        return cls(
            enabled=bool(config.get("enabled", defaults.enabled)),
            kind=kind,
            headless=bool(config.get("headless", defaults.headless)),
            executable_path=executable,
            args=_as_str_tuple(config.get("args")),
            no_sandbox=bool(config.get("no_sandbox", defaults.no_sandbox)),
            user_agent=user_agent,
            viewport_width=_as_int(viewport.get("width"), defaults.viewport_width),
            viewport_height=_as_int(viewport.get("height"), defaults.viewport_height),
            page_load_timeout=_as_float(timeout.get("page_load"), defaults.page_load_timeout),
            script_timeout=_as_float(timeout.get("script_exec"), defaults.script_timeout),
            wait_until=wait_until,
            block_resources=block_resources,
            max_pages=_as_int(config.get("max_pages"), defaults.max_pages),
            allow_scripts=bool(config.get("allow_scripts", defaults.allow_scripts)),
            proxy_gateway=gateway,
            proxy_bypass=_as_str_tuple(proxy.get("bypass_list")),
        )


__all__ = ["BLOCKABLE_RESOURCES", "BROWSER_KINDS", "WAIT_UNTIL_CHOICES", "BrowserSettings"]
