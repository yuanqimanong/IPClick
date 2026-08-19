from __future__ import annotations

from dataclasses import dataclass
import importlib
import os
from pathlib import Path
import sys
import threading
from typing import Any, final

from ipclick.adapters.browser_settings import BrowserSettings, describe_max_pages
from ipclick.exceptions import AdapterError, ConfigError
from ipclick.utils import module_probe
from ipclick.utils.log_util import log


_UNPROBED: Any = object()

_playwright_api: Any = _UNPROBED
_patchright_api: Any = _UNPROBED
_camoufox_new_browser: Any = _UNPROBED
_camoufox_not_installed: Any = _UNPROBED


class _NeverRaised(Exception):
    pass


def _playwright_async_api() -> Any:
    global _playwright_api
    if _playwright_api is _UNPROBED:
        try:
            from playwright.async_api import async_playwright

            _playwright_api = async_playwright
        except ImportError:
            _playwright_api = None
    return _playwright_api


def _patchright_async_api() -> Any:
    global _patchright_api
    if _patchright_api is _UNPROBED:
        try:
            from patchright.async_api import async_playwright

            _patchright_api = async_playwright
        except ImportError:
            _patchright_api = None
    return _patchright_api


def _camoufox_async_new_browser() -> Any:
    global _camoufox_new_browser
    if _camoufox_new_browser is _UNPROBED:
        try:
            from camoufox import AsyncNewBrowser

            _camoufox_new_browser = AsyncNewBrowser
        except ImportError:
            _camoufox_new_browser = None
    return _camoufox_new_browser


def _camoufox_not_installed_error() -> type[Exception]:
    global _camoufox_not_installed
    if _camoufox_not_installed is _UNPROBED:
        try:
            from camoufox.exceptions import CamoufoxNotInstalled

            _camoufox_not_installed = CamoufoxNotInstalled
        except ImportError:
            _camoufox_not_installed = _NeverRaised
    return _camoufox_not_installed or _NeverRaised


ENGINE_MODULES: dict[str, tuple[str, ...]] = {
    "playwright": ("playwright",),
    "patchright": ("patchright",),
    "camoufox": ("camoufox", "playwright"),
    "drissionpage": ("DrissionPage",),
}


INSTALL_HINTS: dict[str, str] = {
    "playwright": 'pip install "ipclick[playwright]" && playwright install chromium',
    "patchright": 'pip install "ipclick[patchright]" && patchright install chromium',
    "camoufox": 'pip install "ipclick[camoufox]" && python -m camoufox fetch',
    "drissionpage": 'pip install "ipclick[drissionpage]"（还需本机已装 Chrome/Chromium）',
}

PLAYWRIGHT_FAMILY: frozenset[str] = frozenset({"playwright", "patchright", "camoufox"})

ENGINE_NAMES: frozenset[str] = PLAYWRIGHT_FAMILY | {"drissionpage"}

FINGERPRINT_MANAGED: frozenset[str] = frozenset({"camoufox"})


def default_engine() -> str:
    return "drissionpage" if sys.platform == "win32" else "camoufox"


def resolve_engine(name: str | None) -> str:
    engine = (name or "auto").strip().lower()
    if engine in ("", "auto"):
        return default_engine()
    if engine not in ENGINE_NAMES:
        raise ConfigError(f"未知的浏览器引擎 {engine!r}，可选：auto、{'、'.join(sorted(ENGINE_NAMES))}")
    return engine


def package_installed(engine: str) -> bool:
    modules = ENGINE_MODULES.get(engine)
    if not modules:
        return False
    return all(module_probe.installed(name) for name in modules)


def refresh() -> None:
    module_probe.invalidate()
    with _browser_ready_lock:
        _browser_ready_cache.clear()


_browser_ready_cache: dict[tuple[str, str, str, str], tuple[bool | None, str]] = {}
_browser_ready_lock = threading.Lock()


def browser_ready(engine: str, settings: BrowserSettings | None = None) -> tuple[bool | None, str]:
    resolved = settings or BrowserSettings()
    key = (
        engine,
        resolved.executable_path or "",
        resolved.kind,
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""),
    )
    cached = _browser_ready_cache.get(key)
    if cached is not None:
        return cached

    result = _probe_browser(engine, resolved)
    with _browser_ready_lock:
        _browser_ready_cache[key] = result
    return result


def _probe_browser(engine: str, resolved: BrowserSettings) -> tuple[bool | None, str]:
    if resolved.executable_path:
        exists = os.path.isfile(resolved.executable_path)
        return exists, f"[BROWSER].executable_path = {resolved.executable_path}" + ("" if exists else "（文件不存在）")

    if engine == "camoufox":
        return _camoufox_browser_ready()
    if engine in ("playwright", "patchright"):
        return _playwright_browser_ready(engine, resolved.kind)
    if engine == "drissionpage":
        return _system_chrome_ready()
    return None, ""


def _resolve_camoufox_binary() -> str:
    from camoufox.pkgman import LAUNCH_FILE, OS_NAME, camoufox_path

    root = camoufox_path(download_if_missing=False)
    if OS_NAME == "mac":
        return os.path.abspath(root / "Camoufox.app" / "Contents" / "Resources" / LAUNCH_FILE[OS_NAME])
    return str(root / LAUNCH_FILE[OS_NAME])


def _camoufox_browser_ready() -> tuple[bool | None, str]:
    try:
        path = _resolve_camoufox_binary()
    except ImportError:
        return None, "camoufox 包未安装"
    except Exception as e:
        return False, f"未下载（{type(e).__name__}），需要 python -m camoufox fetch"
    if not os.path.isfile(path):
        return False, f"{path} 不存在，需要 python -m camoufox fetch"
    return True, path


def playwright_registry_dir(engine: str) -> Path | None:
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if env == "0":
        try:
            module = importlib.import_module(engine)
        except ImportError:
            return None
        root = Path(str(module.__file__)).parent / "driver" / "package"
        return root / ".local-browsers"
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Caches")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "ms-playwright"


def _playwright_browser_ready(engine: str, kind: str) -> tuple[bool | None, str]:
    directory = playwright_registry_dir(engine)
    if directory is None:
        return None, "无法确定浏览器下载目录"
    if not directory.exists():
        return False, f"{directory} 不存在（未执行 {engine} install）"
    try:
        first_level = sorted(directory.iterdir())
        for entry in first_level:
            if entry.is_dir() and entry.name.startswith(kind):
                return True, str(entry)
        for parent in first_level:
            if not parent.is_dir():
                continue
            for entry in sorted(parent.iterdir()):
                if entry.is_dir() and entry.name.startswith(kind):
                    return True, str(entry)
    except OSError as e:
        return None, f"无法读取 {directory}：{e}"
    return False, f"{directory} 里没有 {kind}（未执行 {engine} install {kind}）"


def _system_chrome_ready() -> tuple[bool | None, str]:
    import shutil

    for name in ("chrome", "google-chrome", "chromium", "chromium-browser", "msedge"):
        found = shutil.which(name)
        if found:
            return True, found
    if sys.platform == "win32":
        for candidate in (
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ):
            if os.path.isfile(candidate):
                return True, candidate
    if sys.platform == "darwin":
        candidate = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if os.path.isfile(candidate):
            return True, candidate
    return None, "PATH 里没找到 Chrome/Chromium；若装在别处请设 [BROWSER].executable_path"


@final
@dataclass(frozen=True)
class EngineStatus:
    engine: str
    package: bool
    browser: bool | None
    detail: str = ""

    @property
    def ready(self) -> bool:
        return self.package and self.browser is not False

    @property
    def label(self) -> str:
        if not self.package:
            return "包未安装"
        if self.browser is False:
            return "包已装，浏览器本体未就绪"
        if self.browser is None:
            return "包已装，本体未知"
        return "可用"


def engine_status(engine: str, settings: BrowserSettings | None = None) -> EngineStatus:
    package = package_installed(engine)
    if not package:
        return EngineStatus(engine=engine, package=False, browser=None, detail=INSTALL_HINTS.get(engine, "缺少依赖"))
    browser, detail = browser_ready(engine, settings)
    return EngineStatus(engine=engine, package=True, browser=browser, detail=detail)


def is_available(engine: str, settings: BrowserSettings | None = None) -> bool:
    return engine_status(engine, settings).ready


def available_engines(settings: BrowserSettings | None = None) -> list[str]:
    return sorted(e for e in ENGINE_NAMES if is_available(e, settings))


@dataclass(frozen=True)
class LaunchedBrowser:
    driver: Any
    browser: Any


def _chromium_launch_options(settings: BrowserSettings) -> dict[str, Any]:
    args = list(settings.args)
    if settings.no_sandbox:
        args += ["--no-sandbox", "--disable-dev-shm-usage"]

    options: dict[str, Any] = {"headless": settings.headless}
    if args:
        options["args"] = args
    if settings.executable_path:
        options["executable_path"] = settings.executable_path

    return options


async def launch(engine: str, settings: BrowserSettings) -> LaunchedBrowser:
    status = engine_status(engine, settings)
    if not status.ready:
        raise AdapterError(
            f"浏览器引擎 {engine!r} 不可用（{status.label}）："
            f"{status.detail or INSTALL_HINTS.get(engine, '缺少依赖')}。"
            f"安装：{INSTALL_HINTS.get(engine, '')}"
        )

    if engine == "camoufox":
        return await _launch_camoufox(settings)
    if engine == "patchright":
        return await _launch_playwright_like(_patchright_async_api(), "patchright", settings)
    if engine == "playwright":
        return await _launch_playwright_like(_playwright_async_api(), "playwright", settings)
    raise AdapterError(f"引擎 {engine!r} 不走 Playwright 通路，不该到这里")


async def _launch_playwright_like(api: Any, engine: str, settings: BrowserSettings) -> LaunchedBrowser:
    driver = await api().start()
    try:
        browser = await getattr(driver, settings.kind).launch(**_chromium_launch_options(settings))
    except Exception as e:
        await driver.stop()
        raise AdapterError(f"浏览器启动失败（{engine} / {settings.kind}）：{e}。{INSTALL_HINTS[engine]}") from e

    log.info(
        f"{engine} 浏览器已启动：{settings.kind}, headless={settings.headless}, "
        f"页面上限 {describe_max_pages(settings.max_pages, engine)}"
    )
    return LaunchedBrowser(driver=driver, browser=browser)


def _camoufox_executable() -> str:
    try:
        path = _resolve_camoufox_binary()
    except ImportError as e:
        raise AdapterError(f"camoufox 包未安装：{INSTALL_HINTS['camoufox']}") from e
    except Exception as e:
        raise AdapterError(
            f"Camoufox 浏览器本体未就绪（{type(e).__name__}: {e}）。"
            f"它不随 pip 包一起装，需要单独下载（约 1 GB）：python -m camoufox fetch"
        ) from e
    if not os.path.isfile(path):
        raise AdapterError(f"Camoufox 浏览器本体不存在：{path}。需要单独下载（约 1 GB）：python -m camoufox fetch")
    return path


async def _launch_camoufox(settings: BrowserSettings) -> LaunchedBrowser:
    if settings.kind != "firefox":
        log.debug(f"camoufox 只有 Firefox 内核，忽略 [BROWSER].browser = {settings.kind!r}")

    options: dict[str, Any] = {"headless": settings.headless}
    if settings.args:
        options["args"] = list(settings.args)
    options["executable_path"] = settings.executable_path or _camoufox_executable()
    if settings.locale:
        options["locale"] = settings.locale
    if settings.humanize:
        options["humanize"] = settings.humanize
    if settings.proxy_gateway:
        options["proxy"] = {"server": settings.proxy_gateway}
        if settings.geoip:
            options["geoip"] = True

    not_installed = _camoufox_not_installed_error()

    driver = await _playwright_async_api()().start()
    try:
        browser = await _camoufox_async_new_browser()(driver, **options)
    except not_installed as e:
        await driver.stop()
        raise AdapterError(f"Camoufox 浏览器本体未就绪：{e}。请先执行 python -m camoufox fetch") from e
    except Exception as e:
        await driver.stop()
        raise AdapterError(f"Camoufox 启动失败：{e}。{INSTALL_HINTS['camoufox']}") from e

    log.info(
        f"camoufox 浏览器已启动：headless={settings.headless}, "
        f"页面上限 {describe_max_pages(settings.max_pages, 'camoufox')}"
    )
    return LaunchedBrowser(driver=driver, browser=browser)


__all__ = [
    "ENGINE_MODULES",
    "ENGINE_NAMES",
    "FINGERPRINT_MANAGED",
    "INSTALL_HINTS",
    "PLAYWRIGHT_FAMILY",
    "EngineStatus",
    "LaunchedBrowser",
    "available_engines",
    "browser_ready",
    "default_engine",
    "engine_status",
    "is_available",
    "launch",
    "package_installed",
    "playwright_registry_dir",
    "refresh",
    "resolve_engine",
]
