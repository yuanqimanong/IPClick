from dataclasses import dataclass, field
import os
from typing import Any


BROWSER_KINDS: frozenset[str] = frozenset({"chromium", "firefox", "webkit"})

WAIT_UNTIL_CHOICES: frozenset[str] = frozenset({"load", "domcontentloaded", "networkidle", "commit"})

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

    wait_until: str = "load"

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

        humanize_raw = config.get("humanize", defaults.humanize)
        if isinstance(humanize_raw, bool):
            humanize: bool | float = humanize_raw
        else:
            humanize = _as_float(humanize_raw, 0.0)
            humanize = humanize if humanize > 0 else False

        return cls(
            enabled=bool(config.get("enabled", defaults.enabled)),
            engine=str(config.get("engine", defaults.engine)).strip().lower() or defaults.engine,
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
            max_pages=_as_int(config.get("max_pages"), defaults.max_pages, minimum=0),
            allow_scripts=bool(config.get("allow_scripts", defaults.allow_scripts)),
            proxy_gateway=gateway,
            proxy_bypass=_as_str_tuple(proxy.get("bypass_list")),
            locale=str(config.get("locale") or "").strip() or None,
            humanize=humanize,
            geoip=bool(config.get("geoip", defaults.geoip)),
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

    if not limits:
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
