"""浏览器渲染参数。

把配置文件的 ``[BROWSER]`` 节变成浏览器适配器真正会读的对象。

原来那一节写了一堆配置——插件目录、``.crx``/``.dll`` 列表、缓存上限 MB 数、
``sandbox.level = strict/moderate/off``——没有任何一项有消费方，也没有任何一项
能落到 playwright 上。这里只保留能真正生效的键，其余全部删掉：和
``[DOWNLOADER]`` 一样的原则，宁可少配也不要"配了不生效"。
"""

from dataclasses import dataclass, field
import os
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

    #: 渲染引擎：auto / camoufox / patchright / playwright / drissionpage。
    #: auto 按平台选（Windows -> drissionpage，Linux/macOS -> camoufox），
    #: 解析逻辑在 browser_engines.resolve_engine（放在那边是为了避免循环导入）。
    engine: str = "auto"

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

    #: 同时打开的页面上限。``0`` = 按本机内存自动推导（见 :func:`resolve_max_pages`）。
    #:
    #: 默认仍是 4 而不是 0——自动推导会因机器而异，静默改变既有部署的行为不合适。
    #: 想让它跟着机器走就显式写 0。
    max_pages: int = 4

    #: 是否允许请求携带 automation_script（在页面里执行任意 JS）。
    #: 默认关闭：页面 JS 能绕过服务端的 URL 策略去访问内网，等于把 SSRF 防线
    #: 整个让开。需要时由部署方显式打开，并自行确保只有可信调用方能访问。
    allow_scripts: bool = False

    # 代理
    proxy_gateway: str | None = None
    proxy_bypass: tuple[str, ...] = field(default_factory=tuple)

    # ---- 仅 camoufox ----
    #: 伪装的语言环境，如 "zh-CN"。留空则由 camoufox 自行生成。
    locale: str | None = None
    #: 模拟人类的鼠标移动。True 用默认时长，也可以给一个秒数上限。
    #: 会显著拖慢每次请求，默认关闭。
    humanize: bool | float = False
    #: 让时区 / 语言 / 经纬度与代理出口 IP 对上。
    #: 只有配置级代理（proxy_gateway）能生效——请求级代理是在 context 上设的，
    #: 那时指纹早就生成完了。
    geoip: bool = False

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
            max_pages=_as_int(config.get("max_pages"), defaults.max_pages),
            allow_scripts=bool(config.get("allow_scripts", defaults.allow_scripts)),
            proxy_gateway=gateway,
            proxy_bypass=_as_str_tuple(proxy.get("bypass_list")),
            locale=str(config.get("locale") or "").strip() or None,
            humanize=humanize,
            geoip=bool(config.get("geoip", defaults.geoip)),
        )


__all__ = ["BLOCKABLE_RESOURCES", "BROWSER_KINDS", "WAIT_UNTIL_CHOICES", "BrowserSettings"]


#: 每个并发页面的内存预算（MB），按引擎区分。
#:
#: 这些不是精确值，是**保守的量级**：真实占用随页面复杂度浮动很大（一个静态
#: 页几十 MB，一个重 JS 的应用能到几百）。取保守值的理由是失败代价不对称——
#: 少开一个页面只是慢一点，多开一个把机器推进 swap 就是请求从几秒变几分钟、
#: 看起来像卡死，而且很难从现象联想到是并发开太大。
#:
#: camoufox 明显更贵：它是 Firefox 加一整套反检测扩展，单个 context 的开销
#: 高于 Chromium 系。配置注释里"内存紧张的机器（≤4GB）建议设成 1~2"说的
#: 就是它。
ENGINE_PAGE_BUDGET_MB: dict[str, int] = {
    "camoufox": 400,
    "playwright": 250,
    "patchright": 250,
    "drissionpage": 250,
}
DEFAULT_PAGE_BUDGET_MB = 300

#: 自动推导时给系统留的余量（MB）。把内存吃到一滴不剩比少开几个页面糟得多。
MEMORY_HEADROOM_MB = 1024

#: 自动推导的硬上限。再多的页面收益也会被目标站点和网络吃掉，
#: 而每个页面都是一份实打实的内存。
MAX_AUTO_PAGES = 16


def available_memory_mb() -> int | None:
    """本机可用内存（MB）。读不出来返回 None，由调用方回落到静态默认值。

    优先读 cgroup 的限额而不是宿主机总量：容器里 ``/proc/meminfo`` 报的是
    **宿主机**的内存，照它推导会在一个 512MB 的容器里开出十几个页面，
    然后被 OOM killer 杀掉——而现象只是"容器莫名其妙重启"。
    """
    import os
    from pathlib import Path as _Path

    limits: list[int] = []

    # cgroup v2 → v1，取能读到的那个
    for path, current in (
        ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current"),
        ("/sys/fs/cgroup/memory/memory.limit_in_bytes", "/sys/fs/cgroup/memory/memory.usage_in_bytes"),
    ):
        try:
            raw = _Path(path).read_text(encoding="utf-8").strip()
            if raw in ("max", ""):
                continue
            total = int(raw)
            # 没设限时内核会报一个天文数字，那等于"不受限"
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
    """算出真正生效的页面并发上限。

    ``configured > 0`` 就照用——显式配置永远优先，自动推导不该覆盖人的决定。

    ``configured == 0`` 时按**内存**推导，而不是 CPU 核数。这一点容易搞反：
    浏览器页面是内存瓶颈不是 CPU 瓶颈，按核数算的话一台 16 核 4GB 的机器会
    开出 16 个 camoufox 页面（约 6.4GB），直接换页。CPU 核数只做上限——
    页面渲染确实要 CPU，开得比核数多也没意义。
    """
    if configured > 0:
        return configured

    budget = ENGINE_PAGE_BUDGET_MB.get(engine.lower(), DEFAULT_PAGE_BUDGET_MB)
    available = available_memory_mb()
    if available is None:
        # 读不出内存就退回静态默认值，别自作聪明
        return BrowserSettings.max_pages

    usable = max(0, available - MEMORY_HEADROOM_MB)
    by_memory = usable // budget
    by_cpu = os.cpu_count() or 1
    return max(1, min(by_memory, by_cpu, MAX_AUTO_PAGES))
