from dataclasses import dataclass, field
import os
import sys
from typing import Any

from ipclick.utils.coerce import as_bool, as_int, as_optional_text, as_positive_float, as_text, as_text_tuple
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
        return {"width": self.viewport_width, "height": self.viewport_height}

    @classmethod
    def from_config(cls, browser_config: dict[str, Any] | None) -> "BrowserSettings":
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
            settle_timeout=as_positive_float(timeout.get("settle"), defaults.settle_timeout),
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


__all__ = [
    "BLOCKABLE_RESOURCES",
    "BROWSER_KINDS",
    "WAIT_UNTIL_CHOICES",
    "BrowserSettings",
    "describe_max_pages",
    "resolve_max_pages",
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
    if configured > 0:
        return configured

    budget = ENGINE_PAGE_BUDGET_MB.get(engine.lower(), DEFAULT_PAGE_BUDGET_MB)
    available = available_memory_mb()
    if available is None:
        return BrowserSettings.max_pages

    usable = max(0, available - MEMORY_HEADROOM_MB)
    by_memory = usable // budget
    by_cpu = os.cpu_count() or 1
    return max(1, min(by_memory, by_cpu, MAX_AUTO_PAGES))


def describe_max_pages(configured: int, engine: str) -> str:
    resolved = resolve_max_pages(configured, engine)
    return str(resolved) if configured > 0 else f"{resolved}（auto）"
