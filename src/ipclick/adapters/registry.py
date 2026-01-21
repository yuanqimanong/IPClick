from ipclick.adapters.base import DownloaderAdapter
from ipclick.adapters.curl_cffi_adapter import CurlCffiAdapter
from ipclick.adapters.httpx_adapter import HttpxAdapter
from ipclick.utils.log_util import log


ADAPTER_CLASSES = {
    "curl_cffi": CurlCffiAdapter,
    "httpx": HttpxAdapter,
}

# 可选：列表形式（如果只需要顺序列表）
ADAPTER_LIST = list(ADAPTER_CLASSES.values())


def get_default_adapter() -> DownloaderAdapter:
    return ADAPTER_LIST[0]()


def get_adapter(adapter_name: str) -> DownloaderAdapter:
    """
    获取适配器实例

    Args:
        adapter_name:  适配器名称

    Returns:
        Downloader: 适配器实例
    """
    if adapter_name not in ADAPTER_CLASSES:
        raise RuntimeError(f"下载器适配器 {adapter_name} 尚未支持")

        # TODO 应用配置
        # self._configure_adapter(adapter)

        log.debug(f"Created adapter instance: {adapter_name}")

    return ADAPTER_CLASSES[adapter_name]()
