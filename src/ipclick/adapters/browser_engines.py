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
import importlib
import os
from pathlib import Path
import sys
from typing import Any, final

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


class _NeverRaised(Exception):
    """camoufox 未安装时的占位异常类型，让对应的 except 分支永远不命中。"""


#: camoufox 报"浏览器本体没下"用的异常。没装 camoufox 时是个永不被抛的占位类型，
#: 这样 except 分支不必写 if。
_CamoufoxNotInstalled: type[Exception] = _NeverRaised
try:
    from camoufox.exceptions import CamoufoxNotInstalled as _real_not_installed

    _CamoufoxNotInstalled = _real_not_installed
except ImportError:  # pragma: no cover - 取决于安装环境
    pass


#: 引擎名 -> 缺失时的安装提示
INSTALL_HINTS: dict[str, str] = {
    "playwright": 'pip install "ipclick[playwright]" && playwright install chromium',
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


def package_installed(engine: str) -> bool:
    """引擎的 **Python 包** 装了没。

    只回答"能不能 import"这一半。浏览器本体是另一半，见 :func:`browser_ready`——
    两者分开是因为它们缺失时的处理方式不同：包缺了要 pip install，
    浏览器本体缺了要跑各自的 fetch/install 命令。
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


def browser_ready(engine: str, settings: BrowserSettings | None = None) -> tuple[bool | None, str]:
    """**浏览器本体** 就绪没。返回 ``(结果, 说明)``，结果为 None 表示查不出来。

    为什么非要单独查这一项：``pip install camoufox`` 只装几 MB 的 Python 包，
    浏览器本体（本机实测 1.3 GB）是 ``python -m camoufox fetch`` 下的。而 camoufox
    的 ``camoufox_path(download_if_missing=True)`` 是**默认值**——不查的话，第一个
    浏览器请求会在 gRPC 处理线程里开始下 1.3 GB：请求必然超时，并发的多个首请求
    还可能各自触发一次下载，超时返回后下载仍在后台跑，看起来就像"这个引擎坏了"。

    查不出来时返回 None 而不是 False：宁可显示"未知"，也不要把一台装好的机器
    误报成没装（那会让人去重装一遍已经装好的东西）。
    """
    resolved = settings or BrowserSettings()
    # 显式指定了可执行文件就以它为准——这是容器里复用系统浏览器的标准做法，
    # 此时各引擎自己的下载目录有没有东西都不重要。
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
    """算出 camoufox 浏览器本体的可执行文件路径。**绝不触发下载。**

    刻意不用 camoufox 自己的 ``launch_path()``：它内部走
    ``camoufox_path(download_if_missing=True)``——**默认值就是会下载**。也就是说
    连"查一下装没装"都能让它开始下 1 GB。这在 Web 端尤其危险：渲染一次总览页面
    就可能触发下载。

    所以这里显式传 ``download_if_missing=False``，再自己拼出可执行文件名
    （规则照抄它的 ``get_path``：mac 上在 .app 包里，其余平台在根目录）。

    Raises:
        Exception: camoufox 抛的 CamoufoxNotInstalled / UnsupportedVersion 等。
            类型不稳定，由调用方统一转成 AdapterError。
    """
    from camoufox.pkgman import LAUNCH_FILE, OS_NAME, camoufox_path

    root = camoufox_path(download_if_missing=False)
    if OS_NAME == "mac":
        return os.path.abspath(root / "Camoufox.app" / "Contents" / "Resources" / LAUNCH_FILE[OS_NAME])
    return str(root / LAUNCH_FILE[OS_NAME])


def _camoufox_browser_ready() -> tuple[bool | None, str]:
    """camoufox 的检查是**确定的**：能算出路径且文件在，就是就绪。"""
    try:
        path = _resolve_camoufox_binary()
    except ImportError:
        return None, "camoufox 包未安装"
    except Exception as e:
        # CamoufoxNotInstalled / UnsupportedVersion 都在这里——共同含义是"本体没就绪"，
        # 而 camoufox 的异常类型不稳定，按类型 catch 反而更脆
        return False, f"未下载（{type(e).__name__}），需要 python -m camoufox fetch"
    if not os.path.isfile(path):
        return False, f"{path} 不存在，需要 python -m camoufox fetch"
    return True, path


def _playwright_registry_dir(engine: str) -> Path | None:
    """playwright / patchright 放浏览器的目录。

    解析规则照抄它们 node 驱动里的实现（``PLAYWRIGHT_BROWSERS_PATH`` == "0" 时
    装在包目录内，否则用环境变量或按平台的缓存目录）。
    """
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
    """看下载目录里有没有对应内核。

    目录布局在不同 playwright 版本间变过（曾经是 ``ms-playwright/chromium-1234``，
    也见过多一层 ``ms-playwright/b/...``），所以按前缀在两层深度内找，
    而不是写死一层。
    """
    directory = _playwright_registry_dir(engine)
    if directory is None:
        return None, "无法确定浏览器下载目录"
    if not directory.exists():
        return False, f"{directory} 不存在（未执行 {engine} install）"
    try:
        first_level = sorted(directory.iterdir())
        # 先扫第一层，再扫第一层里那些目录的下一层。分两趟写而不是嵌进一个
        # 生成器表达式里：那种写法要读三遍才能看出"两层深度"这件事。
        for entry in first_level:
            if entry.is_dir() and entry.name.startswith(kind):
                return True, str(entry)
        for parent in first_level:
            if not parent.is_dir():
                continue
            for entry in sorted(parent.iterdir()):
                if entry.is_dir() and entry.name.startswith(kind):
                    return True, str(entry)
    except OSError as e:  # pragma: no cover - 权限问题
        return None, f"无法读取 {directory}：{e}"
    return False, f"{directory} 里没有 {kind}（未执行 {engine} install {kind}）"


def _system_chrome_ready() -> tuple[bool | None, str]:
    """DrissionPage 用本机已装的 Chrome/Chromium，不下载任何东西。"""
    import shutil

    for name in ("chrome", "google-chrome", "chromium", "chromium-browser", "msedge"):
        found = shutil.which(name)
        if found:
            return True, found
    if sys.platform == "win32":
        # Windows 上通常不在 PATH 里，查默认安装位置
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
    # PATH 里没有不等于机器上没有（可能装在别处），所以是"未知"而不是"没有"
    return None, "PATH 里没找到 Chrome/Chromium；若装在别处请设 [BROWSER].executable_path"


@final
@dataclass(frozen=True)
class EngineStatus:
    """一个引擎的完整安装状态。"""

    engine: str
    package: bool
    #: 浏览器本体是否就绪。None = 查不出来
    browser: bool | None
    detail: str = ""

    @property
    def ready(self) -> bool:
        """能不能直接拿来用。

        ``browser is None``（查不出来）时按能用处理：宁可让它真启动一次去报错，
        也不要因为检查不到就拒绝一台其实装好了的机器。
        """
        return self.package and self.browser is not False

    @property
    def label(self) -> str:
        """给人看的一句话状态。"""
        if not self.package:
            return "包未安装"
        if self.browser is False:
            return "包已装，浏览器本体未就绪"
        if self.browser is None:
            return "包已装，本体未知"
        return "可用"


def engine_status(engine: str, settings: BrowserSettings | None = None) -> EngineStatus:
    """查一个引擎装到什么程度了。"""
    package = package_installed(engine)
    if not package:
        return EngineStatus(engine=engine, package=False, browser=None, detail=INSTALL_HINTS.get(engine, "缺少依赖"))
    browser, detail = browser_ready(engine, settings)
    return EngineStatus(engine=engine, package=True, browser=browser, detail=detail)


def is_available(engine: str, settings: BrowserSettings | None = None) -> bool:
    """引擎能不能用（包 **与** 浏览器本体）。

    0.3 起把浏览器本体也算进来了。此前只查 Python 包，于是
    ``pip install "ipclick[camoufox]"`` 但没 fetch 的机器上，``config-info`` 与
    Web 端都显示"可用"，而第一次用会卡几分钟下 1.3 GB。
    """
    return engine_status(engine, settings).ready


def available_engines(settings: BrowserSettings | None = None) -> list[str]:
    return sorted(e for e in ENGINE_NAMES if is_available(e, settings))


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
    status = engine_status(engine, settings)
    if not status.ready:
        # 这一步同时挡住了"浏览器本体没下"的情况。挡在这里是刻意的：camoufox 的
        # 默认行为是缺本体就当场开始下（1.3 GB），而这里已经在 gRPC 的请求线程上了。
        raise AdapterError(
            f"浏览器引擎 {engine!r} 不可用（{status.label}）："
            f"{status.detail or INSTALL_HINTS.get(engine, '缺少依赖')}。"
            f"安装：{INSTALL_HINTS.get(engine, '')}"
        )

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


def _camoufox_executable() -> str:
    """解析 camoufox 浏览器本体的路径，缺了就报错——绝不触发下载。

    Raises:
        AdapterError: 本体未就绪。
    """
    try:
        path = _resolve_camoufox_binary()
    except ImportError as e:  # pragma: no cover - package_installed 已经挡过
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
    """Camoufox 用的是它自己下载的 Firefox，``[BROWSER].browser`` 在这里没有意义。"""
    if settings.kind != "firefox":
        log.debug(f"camoufox 只有 Firefox 内核，忽略 [BROWSER].browser = {settings.kind!r}")

    options: dict[str, Any] = {"headless": settings.headless}
    if settings.args:
        options["args"] = list(settings.args)
    # **总是**显式传 executable_path，这是关键。
    #
    # 不传的话 camoufox 会自己去 launch_path() -> camoufox_path(download_if_missing=True)
    # 解析路径，缺本体就当场开始下载（本机实测 1.3 GB，2 分钟能下 440 MB）。
    # 那一刻我们已经在 gRPC 的请求线程上：请求必然超时，超时返回后下载还在后台跑。
    # 光在前面加检查不够——我们的检查和它的解析可能不一致（环境变量、多版本目录），
    # 一旦不一致就又会走到下载分支。传路径进去才是结构上排除这条路。
    options["executable_path"] = settings.executable_path or _camoufox_executable()
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
    except _CamoufoxNotInstalled as e:
        # 兜底：launch() 已经查过一次，走到这里说明检查和实际用的路径不一致
        # （比如两次调用之间目录被删了）。绝不能让它退化成静默下载 1.3 GB。
        await driver.stop()
        raise AdapterError(f"Camoufox 浏览器本体未就绪：{e}。请先执行 python -m camoufox fetch") from e
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
    "EngineStatus",
    "LaunchedBrowser",
    "available_engines",
    "browser_ready",
    "default_engine",
    "engine_status",
    "is_available",
    "launch",
    "package_installed",
    "resolve_engine",
]
