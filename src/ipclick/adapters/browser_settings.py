"""浏览器渲染配置及基于可用内存的页面并发估算。"""

from dataclasses import dataclass, field
import json as jsonlib
import math
import os
import sys
from typing import Any

from ipclick.exceptions import ValidationError
from ipclick.utils.coerce import as_bool, as_float, as_int, as_optional_text, as_positive_float, as_text, as_text_tuple
from ipclick.utils.config_util import section


BROWSER_KINDS: frozenset[str] = frozenset({"chromium", "firefox", "webkit"})

WAIT_UNTIL_CHOICES: frozenset[str] = frozenset({"load", "domcontentloaded", "networkidle", "commit"})

BLOCKABLE_RESOURCES: frozenset[str] = frozenset(
    {"image", "media", "font", "stylesheet", "script", "xhr", "fetch", "websocket", "other"}
)


def _one_of(value: object, allowed: frozenset[str], default: str) -> str:
    text = as_text(value, default).lower()
    return text if text in allowed else default


def _humanize(value: object) -> bool | float:
    if isinstance(value, bool):
        return value
    seconds = as_positive_float(value, 0.0)
    return seconds if seconds > 0 else False


@dataclass(frozen=True)
class BrowserSettings:
    """跨浏览器引擎共享的不可变运行参数。"""

    enabled: bool = True

    engine: str = "auto"

    kind: str = "chromium"
    headless: bool = True
    executable_path: str | None = None
    args: tuple[str, ...] = ()
    no_sandbox: bool = False

    user_agent: str | None = None
    viewport_width: int = 1920
    viewport_height: int = 1080

    page_load_timeout: float = 30.0
    script_timeout: float = 60.0
    settle_timeout: float = 5.0

    wait_until: str = "networkidle"

    block_resources: tuple[str, ...] = ("image", "media", "font")

    max_pages: int = 4

    allow_scripts: bool = False

    proxy_gateway: str | None = None
    proxy_bypass: tuple[str, ...] = field(default_factory=tuple)

    locale: str | None = None
    humanize: bool | float = False
    geoip: bool = False

    @property
    def viewport(self) -> dict[str, int]:
        """返回 Playwright context 使用的视口字典。"""
        return {"width": self.viewport_width, "height": self.viewport_height}

    @classmethod
    def from_config(cls, browser_config: dict[str, Any] | None) -> "BrowserSettings":
        """从 ``[BROWSER]`` 配置解析并规范化浏览器参数。"""
        config = dict(browser_config or {})
        timeout = section(config, "timeout")
        proxy = section(config, "proxy")
        viewport = section(config, "viewport")

        defaults = cls()

        blocked = config.get("block_resources")
        block_resources = (
            as_text_tuple(blocked, BLOCKABLE_RESOURCES) if blocked is not None else defaults.block_resources
        )

        return cls(
            enabled=as_bool(config.get("enabled"), defaults.enabled),
            engine=as_text(config.get("engine"), defaults.engine).lower(),
            kind=_one_of(config.get("browser"), BROWSER_KINDS, defaults.kind),
            headless=as_bool(config.get("headless"), defaults.headless),
            executable_path=as_optional_text(config.get("executable_path")),
            args=as_text_tuple(config.get("args")),
            no_sandbox=as_bool(config.get("no_sandbox"), defaults.no_sandbox),
            user_agent=as_optional_text(config.get("user_agent")),
            viewport_width=as_int(viewport.get("width"), defaults.viewport_width, minimum=1),
            viewport_height=as_int(viewport.get("height"), defaults.viewport_height, minimum=1),
            page_load_timeout=as_positive_float(timeout.get("page_load"), defaults.page_load_timeout),
            script_timeout=as_positive_float(timeout.get("script_exec"), defaults.script_timeout),
            # settle=0 明确表示不额外等待 networkidle；负数和非有限值回退默认值。
            settle_timeout=as_float(timeout.get("settle"), defaults.settle_timeout, minimum=0.0),
            wait_until=_one_of(config.get("wait_until"), WAIT_UNTIL_CHOICES, defaults.wait_until),
            block_resources=block_resources,
            max_pages=as_int(config.get("max_pages"), defaults.max_pages, minimum=0),
            allow_scripts=as_bool(config.get("allow_scripts"), defaults.allow_scripts),
            proxy_gateway=as_optional_text(proxy.get("gateway")),
            proxy_bypass=as_text_tuple(proxy.get("bypass_list")),
            locale=as_optional_text(config.get("locale")),
            humanize=_humanize(config.get("humanize", defaults.humanize)),
            geoip=as_bool(config.get("geoip"), defaults.geoip),
        )


# automation_config 里等待类字段的硬上限。放在这里而不是各适配器里：两个浏览器适配器
# 原来各自定义了一份同值常量，改一处漏一处。
MAX_WAIT_FOR_TIMEOUT_MS = 60_000

# 滚到底的页内语句。必须带 document.body 判空：XML / 纯 SVG / text-plain 文档没有 body，
# 少了它会抛 TypeError，被当成用户脚本错误报成 ValidationError。
SCROLL_TO_BOTTOM_JS = "if (document.body) window.scrollTo(0, document.body.scrollHeight);"

# 读文档高度的页内语句，同样要判空。
DOCUMENT_HEIGHT_JS = "document.body ? document.body.scrollHeight : 0"


def parse_automation_config(automation_config: str | None) -> dict[str, Any]:
    """把 automation_config 的 JSON 文本解析成字典；两个浏览器适配器共用。"""
    if not automation_config:
        return {}
    try:
        parsed = jsonlib.loads(automation_config)
    except (TypeError, ValueError) as e:
        raise ValidationError(f"automation_config 不是合法的 JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise ValidationError(f"automation_config 必须是 JSON 对象，收到 {type(parsed).__name__}")
    return parsed


def positive_number(config: dict[str, Any], key: str) -> int:
    """读 automation_config 里的非负整数字段；非数字与非有限值明确报错。"""
    raw = config.get(key)
    if raw is None or raw == "":
        return 0
    try:
        value = float(raw)
    except (TypeError, ValueError) as e:
        raise ValidationError(f"automation_config.{key} 必须是数字，收到 {raw!r}") from e
    if not math.isfinite(value):
        raise ValidationError(f"automation_config.{key} 必须是有限数字，收到 {raw!r}")
    return max(0, int(value))


def wait_for_timeout_ms(config: dict[str, Any]) -> int:
    """读 wait_for_timeout 并钳到上限；两个浏览器适配器共用同一口径。"""
    return min(MAX_WAIT_FOR_TIMEOUT_MS, positive_number(config, "wait_for_timeout"))


__all__ = [
    "BLOCKABLE_RESOURCES",
    "BROWSER_KINDS",
    "DOCUMENT_HEIGHT_JS",
    "MAX_WAIT_FOR_TIMEOUT_MS",
    "SCROLL_TO_BOTTOM_JS",
    "WAIT_UNTIL_CHOICES",
    "BrowserSettings",
    "describe_max_pages",
    "parse_automation_config",
    "positive_number",
    "resolve_max_pages",
    "wait_for_timeout_ms",
]


ENGINE_PAGE_BUDGET_MB: dict[str, int] = {
    "camoufox": 400,
    "playwright": 250,
    "patchright": 250,
    "drissionpage": 250,
}
DEFAULT_PAGE_BUDGET_MB = 300

MEMORY_HEADROOM_MB = 1024

MAX_AUTO_PAGES = 16


def available_memory_mb() -> int | None:
    """返回进程当前可用内存，优先考虑容器 cgroup 限额。"""
    import os
    from pathlib import Path as _Path

    limits: list[int] = []

    for path, current in (
        ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current"),
        ("/sys/fs/cgroup/memory/memory.limit_in_bytes", "/sys/fs/cgroup/memory/memory.usage_in_bytes"),
    ):
        try:
            raw = _Path(path).read_text(encoding="utf-8").strip()
            if raw in ("max", ""):
                continue
            total = int(raw)
            if total <= 0 or total > (1 << 50):
                continue
            used = int(_Path(current).read_text(encoding="utf-8").strip() or 0)
            limits.append(max(0, total - used) // (1024 * 1024))
            break
        except (OSError, ValueError):
            continue

    try:
        meminfo = _Path("/proc/meminfo").read_text(encoding="utf-8")
        for line in meminfo.splitlines():
            if line.startswith("MemAvailable:"):
                limits.append(int(line.split()[1]) // 1024)
                break
    except (OSError, ValueError, IndexError):
        pass

    if not limits and sys.platform != "win32":
        try:
            page_size = os.sysconf("SC_PAGE_SIZE")
            limits.append(os.sysconf("SC_AVPHYS_PAGES") * page_size // (1024 * 1024))
        except (ValueError, OSError, AttributeError):
            return None

    return min(limits) if limits else None


def resolve_max_pages(configured: int, engine: str) -> int:
    """解析显式页面上限，或按内存、CPU 和引擎预算自动估算。"""
    if configured > 0:
        return configured

    budget = ENGINE_PAGE_BUDGET_MB.get(engine.lower(), DEFAULT_PAGE_BUDGET_MB)
    available = available_memory_mb()
    if available is None:
        return BrowserSettings.max_pages

    # 先预留系统余量，避免浏览器并发把容器推到 OOM 临界点。
    usable = max(0, available - MEMORY_HEADROOM_MB)
    by_memory = usable // budget
    by_cpu = os.cpu_count() or 1
    return max(1, min(by_memory, by_cpu, MAX_AUTO_PAGES))


def describe_max_pages(configured: int, engine: str) -> str:
    """返回适合日志展示的页面并发说明。"""
    resolved = resolve_max_pages(configured, engine)
    return str(resolved) if configured > 0 else f"{resolved}（auto）"
