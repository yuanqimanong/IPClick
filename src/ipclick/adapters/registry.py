from typing import cast

from ipclick.adapters.base import DownloaderAdapter
from ipclick.adapters.browser_settings import BrowserSettings
from ipclick.adapters.curl_cffi_adapter import CurlCffiAdapter
from ipclick.adapters.httpx_adapter import HttpxAdapter
from ipclick.adapters.playwright_adapter import PLAYWRIGHT_AVAILABLE, PlaywrightAdapter
from ipclick.adapters.requests_adapter import REQUESTS_AVAILABLE, RequestsAdapter
from ipclick.adapters.settings import AdapterSettings
from ipclick.exceptions import AdapterError


# 已实现的适配器。IPClickAdapter 枚举里还列了 DrissionPage /
# undetected_chromedriver，那些尚未实现，请求到会明确报错。
ADAPTER_CLASSES: dict[str, type[DownloaderAdapter]] = {
    CurlCffiAdapter.adapter_name: CurlCffiAdapter,
    HttpxAdapter.adapter_name: HttpxAdapter,
}

# requests / playwright 是可选依赖，装了才注册——否则 get_adapter() 的报错会是
# "尚未支持"，而真实原因是"没装"，两者的处理方式完全不同。
if REQUESTS_AVAILABLE:
    ADAPTER_CLASSES[RequestsAdapter.adapter_name] = RequestsAdapter
if PLAYWRIGHT_AVAILABLE:
    ADAPTER_CLASSES[PlaywrightAdapter.adapter_name] = PlaywrightAdapter

# 可选：列表形式（如果只需要顺序列表）
ADAPTER_LIST: list[type[DownloaderAdapter]] = list(ADAPTER_CLASSES.values())

DEFAULT_ADAPTER_NAME = CurlCffiAdapter.adapter_name

#: 需要 [BROWSER] 配置的适配器。它们的构造函数多一个 browser_settings 参数。
BROWSER_ADAPTER_NAMES: frozenset[str] = frozenset({PlaywrightAdapter.adapter_name})

#: 声明了但因缺依赖而未注册的适配器，给出安装提示而不是笼统的"尚未支持"
_OPTIONAL_HINTS: dict[str, str] = {
    "requests": 'pip install "ipclick[requests]"',
    "playwright": 'pip install "ipclick[browser]" && playwright install chromium',
}


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
        adapter_name: 适配器名称
        settings: 来自配置文件 ``[DOWNLOADER]`` 节的默认行为；None 表示用内置默认值。
        browser_settings: 来自配置文件 ``[BROWSER]`` 节；只有浏览器适配器会用到。

    Returns:
        DownloaderAdapter: 适配器实例

    Raises:
        AdapterError: 适配器未实现，或其依赖库未安装。
    """
    adapter_class = ADAPTER_CLASSES.get(adapter_name)
    if adapter_class is None:
        supported = ", ".join(sorted(ADAPTER_CLASSES))
        hint = _OPTIONAL_HINTS.get(adapter_name)
        if hint:
            raise AdapterError(f"适配器 {adapter_name!r} 需要额外依赖：{hint}")
        raise AdapterError(f"下载器适配器 {adapter_name!r} 尚未支持，当前可用: {supported}")

    if adapter_name in BROWSER_ADAPTER_NAMES:
        return cast(type[PlaywrightAdapter], adapter_class)(settings, browser_settings)
    return adapter_class(settings)


def register_adapter(adapter_class: type[DownloaderAdapter]) -> None:
    """注册自定义适配器，便于在不改动本包的前提下扩展。"""
    ADAPTER_CLASSES[adapter_class.adapter_name] = adapter_class
    ADAPTER_LIST[:] = list(ADAPTER_CLASSES.values())
