from typing import cast

from ipclick.adapters import browser_engines
from ipclick.adapters.base import DownloaderAdapter
from ipclick.adapters.browser_adapter import ENGINE_ADAPTERS, BrowserAdapter
from ipclick.adapters.browser_settings import BrowserSettings
from ipclick.adapters.curl_cffi_adapter import CurlCffiAdapter
from ipclick.adapters.drission_adapter import DRISSIONPAGE_MODULE, DrissionPageAdapter
from ipclick.adapters.niquests_adapter import NIQUESTS_MODULE, NiquestsAdapter
from ipclick.adapters.settings import AdapterSettings
from ipclick.exceptions import AdapterError, ConfigError
from ipclick.utils import module_probe


# curl_cffi 是唯一的核心适配器：它是默认值，也是唯一带浏览器指纹伪装的。
# 其余全部按需安装，`pip install ipclick` 因此保持轻量。
ADAPTER_CLASSES: dict[str, type[DownloaderAdapter]] = {
    CurlCffiAdapter.adapter_name: CurlCffiAdapter,
}

#: 通用浏览器适配器名："渲染就行，引擎由服务端定"。
#: 解析到哪个引擎取决于 [BROWSER].engine（默认按平台选）。
GENERIC_BROWSER_NAME = "browser"

# 可选：列表形式（如果只需要顺序列表）
ADAPTER_LIST: list[type[DownloaderAdapter]] = list(ADAPTER_CLASSES.values())

DEFAULT_ADAPTER_NAME = CurlCffiAdapter.adapter_name

#: 可选适配器：``适配器名 -> (类, 探测用的顶层模块名)``。
#:
#: 浏览器系用 ``package_installed`` 的等价物（只看 Python 包）而不是
#: ``is_available``（还要求浏览器本体已就绪）：本体没下时仍然注册，是为了让报错
#: 停在"引擎 X 的浏览器本体未就绪，请执行 camoufox fetch"，而不是退化成"尚未
#: 支持该适配器"——后者会让人去查错方向。
_OPTIONAL_ADAPTERS: dict[str, tuple[type[DownloaderAdapter], tuple[str, ...]]] = {
    NiquestsAdapter.adapter_name: (NiquestsAdapter, (NIQUESTS_MODULE,)),
    DrissionPageAdapter.adapter_name: (DrissionPageAdapter, (DRISSIONPAGE_MODULE,)),
    **{cls.adapter_name: (cls, browser_engines.ENGINE_MODULES[engine]) for engine, cls in ENGINE_ADAPTERS.items()},
}


def sync_optional_adapters() -> None:
    """按**当前磁盘上**的安装状态增删可选适配器的注册。

    0.3 时这段逻辑是模块级的 if，只在 import 那一刻跑一次：运行时装完
    ``ipclick[niquests]`` 也用不上，必须重启进程。现在它是个可以反复调的函数，
    Web 端装完/卸完依赖就调一次。

    只增删 :data:`_OPTIONAL_ADAPTERS` 里登记过的名字，不碰
    :func:`register_adapter` 注册进来的自定义适配器——那些不归这里管。
    """
    for name, (cls, modules) in _OPTIONAL_ADAPTERS.items():
        if all(module_probe.installed(m) for m in modules):
            ADAPTER_CLASSES[name] = cls
        else:
            _ = ADAPTER_CLASSES.pop(name, None)
    ADAPTER_LIST[:] = list(ADAPTER_CLASSES.values())


def refresh() -> None:
    """重新探测所有可选依赖的安装状态，并同步适配器注册表。

    这是"不重启进程也能反映安装/卸载"的总入口：先让探测缓存失效，
    再按新结论增删注册。
    """
    browser_engines.refresh()
    sync_optional_adapters()


sync_optional_adapters()

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
    "niquests": 'pip install "ipclick[niquests]"',
    **{cls.adapter_name: browser_engines.INSTALL_HINTS[engine] for engine, cls in ENGINE_ADAPTERS.items()},
    DrissionPageAdapter.adapter_name: browser_engines.INSTALL_HINTS["drissionpage"],
}

#: **已移除**的适配器 -> 该改用什么。
#:
#: 和上面那张表分开，因为两者的正确说法完全不同：缺依赖是"装一下就能用"，
#: 已移除是"装什么都没用，请改配置"。混在一起会打出"适配器 'httpx' 需要额外
#: 依赖：httpx 适配器已移除"这种自相矛盾的话。
#:
#: 枚举值在 proto 里保留（标了 deprecated）不复用，所以旧客户端发来这些名字时
#: 能走到这里拿到一句有用的话，而不是"未知的适配器枚举值"。
_REMOVED_ADAPTERS: dict[str, str] = {
    "requests": '请改用 niquests：API 相同，且支持 HTTP/2 与 HTTP/3。pip install "ipclick[niquests]"',
    "httpx": '请改用 niquests：能力覆盖 httpx（并多支持 HTTP/3）。pip install "ipclick[niquests]"；'
    "需要浏览器指纹伪装则用默认的 curl_cffi",
    # 从来没实现过，也不打算实现：它基于 selenium + chromedriver，能力和
    # patchright / camoufox 高度重叠，多养一套的收益抵不上维护成本。
    #
    # 放进这张表而不是留着不管，是因为不管的话报的是"尚未支持"——那句话在暗示
    # "以后会有"，于是有人会去等、去提 issue 问什么时候支持。说清楚"不会有，用
    # 这两个"才是实话。
    "undetected_chromedriver": "不会实现（与 patchright / camoufox 能力重叠）。"
    '需要反检测的 Chromium 用 pip install "ipclick[patchright]"，'
    '需要最彻底的指纹伪装用 pip install "ipclick[camoufox]"',
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
        removed = _REMOVED_ADAPTERS.get(adapter_name)
        if removed:
            raise AdapterError(f"适配器 {adapter_name!r} 已移除：{removed}")
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
    "refresh",
    "register_adapter",
    "resolve_browser_adapter_name",
    "sync_optional_adapters",
]
