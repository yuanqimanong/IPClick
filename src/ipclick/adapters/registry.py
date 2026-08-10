from ipclick.adapters.base import DownloaderAdapter
from ipclick.adapters.curl_cffi_adapter import CurlCffiAdapter
from ipclick.adapters.httpx_adapter import HttpxAdapter
from ipclick.adapters.settings import AdapterSettings
from ipclick.exceptions import AdapterError


# 已实现的适配器。IPClickAdapter 枚举里还列了 requests / DrissionPage /
# undetected_chromedriver / playwright，那些尚未实现，请求到会明确报错。
ADAPTER_CLASSES: dict[str, type[DownloaderAdapter]] = {
    CurlCffiAdapter.adapter_name: CurlCffiAdapter,
    HttpxAdapter.adapter_name: HttpxAdapter,
}

# 可选：列表形式（如果只需要顺序列表）
ADAPTER_LIST: list[type[DownloaderAdapter]] = list(ADAPTER_CLASSES.values())

DEFAULT_ADAPTER_NAME = CurlCffiAdapter.adapter_name


def get_default_adapter(settings: AdapterSettings | None = None) -> DownloaderAdapter:
    """创建默认适配器实例（curl_cffi）。"""
    return get_adapter(DEFAULT_ADAPTER_NAME, settings)


def get_adapter(adapter_name: str, settings: AdapterSettings | None = None) -> DownloaderAdapter:
    """
    获取适配器实例

    Args:
        adapter_name: 适配器名称
        settings: 来自配置文件 ``[DOWNLOADER]`` 节的默认行为；None 表示用内置默认值。

    Returns:
        DownloaderAdapter: 适配器实例

    Raises:
        AdapterError: 适配器未实现，或其依赖库未安装。
    """
    adapter_class = ADAPTER_CLASSES.get(adapter_name)
    if adapter_class is None:
        supported = ", ".join(sorted(ADAPTER_CLASSES))
        raise AdapterError(f"下载器适配器 {adapter_name!r} 尚未支持，当前可用: {supported}")

    return adapter_class(settings)


def register_adapter(adapter_class: type[DownloaderAdapter]) -> None:
    """注册自定义适配器，便于在不改动本包的前提下扩展。"""
    ADAPTER_CLASSES[adapter_class.adapter_name] = adapter_class
    ADAPTER_LIST[:] = list(ADAPTER_CLASSES.values())
