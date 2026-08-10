"""浏览器渲染引擎。

四个引擎，其中三个共用同一套代码：

===============  ==========================  ===============================
引擎             底层                        为什么要它
===============  ==========================  ===============================
``camoufox``     Firefox（Playwright API）   反检测做得最彻底，自带指纹伪装
``patchright``   Chromium（Playwright API）  Playwright 的反检测分支，API 全兼容
``playwright``   Chromium/Firefox/WebKit     原版，最稳，行为最可预期
``drissionpage`` Chromium（CDP 直连）        Windows 上生态成熟，不依赖 Playwright
===============  ==========================  ===============================

前三个都产出一个 ``playwright.async_api.Browser``，所以
:mod:`ipclick.adapters.browser_adapter` 那一套线程模型、上下文隔离、资源拦截
全部复用，这里只负责"怎么把浏览器拉起来"。DrissionPage 是另一套 API，
单独实现在 :mod:`ipclick.adapters.drission_adapter`。

平台默认
--------
``[BROWSER].engine = "auto"`` 时按平台选：

* **Windows** → ``drissionpage``
* **Linux / macOS** → ``camoufox``
"""

from __future__ import annotations

from dataclasses import dataclass
import sys
from typing import Any

from ipclick.adapters.browser_settings import BrowserSettings
from ipclick.exceptions import AdapterError, ConfigError
from ipclick.utils.log_util import log


_playwright_api: Any
_patchright_api: Any
_camoufox_new_browser: Any

try:
    from playwright.async_api import async_playwright as _playwright_api
except ImportError:  # pragma: no cover - 取决于安装环境
    _playwright_api = None

try:
    from patchright.async_api import async_playwright as _patchright_api
except ImportError:  # pragma: no cover - 取决于安装环境
    _patchright_api = None

try:
    from camoufox import AsyncNewBrowser as _camoufox_new_browser
except ImportError:  # pragma: no cover - 取决于安装环境
    _camoufox_new_browser = None


#: 引擎名 -> 缺失时的安装提示
INSTALL_HINTS: dict[str, str] = {
    "playwright": 'pip install "ipclick[browser]" && playwright install chromium',
    "patchright": 'pip install "ipclick[patchright]" && patchright install chromium',
    "camoufox": 'pip install "ipclick[camoufox]" && python -m camoufox fetch',
    "drissionpage": 'pip install "ipclick[drissionpage]"（还需本机已装 Chrome/Chromium）',
}

#: 走 Playwright API 的引擎。DrissionPage 不在此列。
PLAYWRIGHT_FAMILY: frozenset[str] = frozenset({"playwright", "patchright", "camoufox"})

#: 全部引擎名
ENGINE_NAMES: frozenset[str] = PLAYWRIGHT_FAMILY | {"drissionpage"}

#: 自带指纹伪装的引擎。对它们不要在 context 上强行覆盖 viewport / User-Agent——
#: 那会和它生成的指纹自相矛盾，反而更容易被识别出来。
FINGERPRINT_MANAGED: frozenset[str] = frozenset({"camoufox"})


def default_engine() -> str:
    """按平台选默认引擎。

    Windows 上 DrissionPage 生态成熟、不用额外下浏览器；Linux/macOS 上
    Camoufox 的反检测能力更强，且它自带的 Firefox 在无头服务器上更好伺候。
    """
    return "drissionpage" if sys.platform == "win32" else "camoufox"


def resolve_engine(name: str | None) -> str:
    """把 ``[BROWSER].engine`` 的取值解析成具体引擎名。"""
    engine = (name or "auto").strip().lower()
    if engine in ("", "auto"):
        return default_engine()
    if engine not in ENGINE_NAMES:
        raise ConfigError(f"未知的浏览器引擎 {engine!r}，可选：auto、{'、'.join(sorted(ENGINE_NAMES))}")
    return engine


def is_available(engine: str) -> bool:
    """引擎依赖是否已安装。

    只检查 Python 包，不检查浏览器二进制——后者要真启动一次才知道，
    那个错误留给 :func:`launch` 报，信息也更具体。
    """
    if engine == "playwright":
        return _playwright_api is not None
    if engine == "patchright":
        return _patchright_api is not None
    if engine == "camoufox":
        # camoufox 是 Playwright 的包装，两者都得在
        return _camoufox_new_browser is not None and _playwright_api is not None
    if engine == "drissionpage":
        from ipclick.adapters.drission_adapter import DRISSIONPAGE_AVAILABLE

        return DRISSIONPAGE_AVAILABLE
    return False


def available_engines() -> list[str]:
    return sorted(e for e in ENGINE_NAMES if is_available(e))


@dataclass(frozen=True)
class LaunchedBrowser:
    """一次启动的产物。关闭时两个都要收，否则 driver 进程会泄漏。"""

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

    # 这里刻意**不**设启动级代理。playwright 文档里那个
    # proxy={"server": "per-context"} 的写法是给旧版 chromium 的：现在每个
    # context 单独设代理已经能直接生效，而一旦设了那个占位值，没配代理的
    # context 会去连一个叫 "per-context" 的代理，所有直连请求全部
    # ERR_PROXY_CONNECTION_FAILED。
    return options


async def launch(engine: str, settings: BrowserSettings) -> LaunchedBrowser:
    """拉起浏览器，返回 (driver, browser)。

    必须在事件循环线程里调用。

    Raises:
        AdapterError: 依赖没装，或浏览器起不来。
    """
    if not is_available(engine):
        raise AdapterError(f"浏览器引擎 {engine!r} 不可用：{INSTALL_HINTS.get(engine, '缺少依赖')}")

    if engine == "camoufox":
        return await _launch_camoufox(settings)
    if engine == "patchright":
        return await _launch_playwright_like(_patchright_api, "patchright", settings)
    if engine == "playwright":
        return await _launch_playwright_like(_playwright_api, "playwright", settings)
    raise AdapterError(f"引擎 {engine!r} 不走 Playwright 通路，不该到这里")


async def _launch_playwright_like(api: Any, engine: str, settings: BrowserSettings) -> LaunchedBrowser:
    driver = await api().start()
    try:
        browser = await getattr(driver, settings.kind).launch(**_chromium_launch_options(settings))
    except Exception as e:
        await driver.stop()
        raise AdapterError(f"浏览器启动失败（{engine} / {settings.kind}）：{e}。{INSTALL_HINTS[engine]}") from e

    log.info(f"{engine} 浏览器已启动：{settings.kind}, headless={settings.headless}, 页面上限 {settings.max_pages}")
    return LaunchedBrowser(driver=driver, browser=browser)


async def _launch_camoufox(settings: BrowserSettings) -> LaunchedBrowser:
    """Camoufox 用的是它自己下载的 Firefox，``[BROWSER].browser`` 在这里没有意义。"""
    if settings.kind != "firefox":
        log.debug(f"camoufox 只有 Firefox 内核，忽略 [BROWSER].browser = {settings.kind!r}")

    options: dict[str, Any] = {"headless": settings.headless}
    if settings.args:
        options["args"] = list(settings.args)
    if settings.executable_path:
        options["executable_path"] = settings.executable_path
    if settings.locale:
        options["locale"] = settings.locale
    if settings.humanize:
        options["humanize"] = settings.humanize
    # geoip 让时区 / 语言 / 经纬度与代理出口 IP 对上。只有配置级代理能这么做——
    # 请求级代理是 context 上设的，那时指纹早已生成。
    if settings.proxy_gateway:
        options["proxy"] = {"server": settings.proxy_gateway}
        if settings.geoip:
            options["geoip"] = True

    driver = await _playwright_api().start()
    try:
        browser = await _camoufox_new_browser(driver, **options)
    except Exception as e:
        await driver.stop()
        raise AdapterError(f"Camoufox 启动失败：{e}。{INSTALL_HINTS['camoufox']}") from e

    log.info(f"camoufox 浏览器已启动：headless={settings.headless}, 页面上限 {settings.max_pages}")
    return LaunchedBrowser(driver=driver, browser=browser)


__all__ = [
    "ENGINE_NAMES",
    "FINGERPRINT_MANAGED",
    "INSTALL_HINTS",
    "PLAYWRIGHT_FAMILY",
    "LaunchedBrowser",
    "available_engines",
    "default_engine",
    "is_available",
    "launch",
    "resolve_engine",
]
