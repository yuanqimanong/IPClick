from typing import cast

from ipclick.adapters import browser_engines
from ipclick.adapters.base import DownloaderAdapter
from ipclick.adapters.browser_adapter import ENGINE_ADAPTERS, BrowserAdapter
from ipclick.adapters.browser_settings import BrowserSettings
from ipclick.adapters.curl_cffi_adapter import CurlCffiAdapter
from ipclick.adapters.drission_adapter import DRISSIONPAGE_AVAILABLE, DrissionPageAdapter
from ipclick.adapters.httpx_adapter import HttpxAdapter
from ipclick.adapters.requests_adapter import REQUESTS_AVAILABLE, RequestsAdapter
from ipclick.adapters.settings import AdapterSettings
from ipclick.exceptions import AdapterError, ConfigError


# 始终可用的 HTTP 适配器（依赖是核心依赖）
ADAPTER_CLASSES: dict[str, type[DownloaderAdapter]] = {
    CurlCffiAdapter.adapter_name: CurlCffiAdapter,
    HttpxAdapter.adapter_name: HttpxAdapter,
}

# 可选依赖：装了才注册。否则 get_adapter() 的报错会是"尚未支持"，
# 而真实原因是"没装"——两者的处理方式完全不同。
if REQUESTS_AVAILABLE:
    ADAPTER_CLASSES[RequestsAdapter.adapter_name] = RequestsAdapter
if DRISSIONPAGE_AVAILABLE:
    ADAPTER_CLASSES[DrissionPageAdapter.adapter_name] = DrissionPageAdapter
for _engine, _cls in ENGINE_ADAPTERS.items():
    if browser_engines.is_available(_engine):
        ADAPTER_CLASSES[_cls.adapter_name] = _cls

#: 通用浏览器适配器名："渲染就行，引擎由服务端定"。
#: 解析到哪个引擎取决于 [BROWSER].engine（默认按平台选）。
GENERIC_BROWSER_NAME = "browser"

# 可选：列表形式（如果只需要顺序列表）
ADAPTER_LIST: list[type[DownloaderAdapter]] = list(ADAPTER_CLASSES.values())

DEFAULT_ADAPTER_NAME = CurlCffiAdapter.adapter_name

#: 需要 [BROWSER] 配置的适配器。它们的构造函数多一个 browser_settings 参数。
BROWSER_ADAPTER_NAMES: frozenset[str] = frozenset(
    {GENERIC_BROWSER_NAME, DrissionPageAdapter.adapter_name} | {c.adapter_name for c in ENGINE_ADAPTERS.values()}
)

#: 引擎名 -> 适配器名。引擎名是配置里写的（小写），适配器名是协议里用的。
_ENGINE_TO_ADAPTER: dict[str, str] = {
    **{engine: cls.adapter_name for engine, cls in ENGINE_ADAPTERS.items()},
    "drissionpage": DrissionPageAdapter.adapter_name,
}

#: 声明了但因缺依赖而未注册的适配器，给出安装提示而不是笼统的"尚未支持"
_OPTIONAL_HINTS: dict[str, str] = {
    "requests": 'pip install "ipclick[requests]"',
    **{cls.adapter_name: browser_engines.INSTALL_HINTS[engine] for engine, cls in ENGINE_ADAPTERS.items()},
    DrissionPageAdapter.adapter_name: browser_engines.INSTALL_HINTS["drissionpage"],
}


def resolve_browser_adapter_name(browser_settings: BrowserSettings | None) -> str:
    """把通用的 ``browser`` 解析成具体的适配器名。

    引擎取自 ``[BROWSER].engine``；``auto``（默认）按平台选：
    Windows → DrissionPage，Linux/macOS → Camoufox。
    """
    engine = browser_engines.resolve_engine((browser_settings or BrowserSettings()).engine)
    return _ENGINE_TO_ADAPTER[engine]


def get_default_adapter(settings: AdapterSettings | None = None) -> DownloaderAdapter:
    """创建默认适配器实例（curl_cffi）。"""
    return get_adapter(DEFAULT_ADAPTER_NAME, settings)


def get_adapter(
    adapter_name: str,
    settings: AdapterSettings | None = None,
    browser_settings: BrowserSettings | None = None,
) -> DownloaderAdapter:
    """
    获取适配器实例

    Args:
        adapter_name: 适配器名称。``browser`` 表示"用浏览器渲染，引擎由服务端定"。
        settings: 来自配置文件 ``[DOWNLOADER]`` 节的默认行为；None 表示用内置默认值。
        browser_settings: 来自配置文件 ``[BROWSER]`` 节；只有浏览器适配器会用到。

    Returns:
        DownloaderAdapter: 适配器实例

    Raises:
        AdapterError: 适配器未实现，或其依赖库未安装。
        ConfigError: ``[BROWSER].engine`` 配置了未知的引擎名。
    """
    if adapter_name == GENERIC_BROWSER_NAME:
        # 解析失败时报的是配置错误（ConfigError），不该被当成"适配器不存在"
        adapter_name = resolve_browser_adapter_name(browser_settings)

    adapter_class = ADAPTER_CLASSES.get(adapter_name)
    if adapter_class is None:
        supported = ", ".join(sorted(ADAPTER_CLASSES))
        hint = _OPTIONAL_HINTS.get(adapter_name)
        if hint:
            raise AdapterError(f"适配器 {adapter_name!r} 需要额外依赖：{hint}")
        raise AdapterError(f"下载器适配器 {adapter_name!r} 尚未支持，当前可用: {supported}")

    if adapter_name in BROWSER_ADAPTER_NAMES:
        return cast(type[BrowserAdapter], adapter_class)(settings, browser_settings)
    return adapter_class(settings)


def register_adapter(adapter_class: type[DownloaderAdapter]) -> None:
    """注册自定义适配器，便于在不改动本包的前提下扩展。"""
    ADAPTER_CLASSES[adapter_class.adapter_name] = adapter_class
    ADAPTER_LIST[:] = list(ADAPTER_CLASSES.values())


__all__ = [
    "ADAPTER_CLASSES",
    "ADAPTER_LIST",
    "BROWSER_ADAPTER_NAMES",
    "DEFAULT_ADAPTER_NAME",
    "GENERIC_BROWSER_NAME",
    "ConfigError",
    "get_adapter",
    "get_default_adapter",
    "register_adapter",
    "resolve_browser_adapter_name",
]
