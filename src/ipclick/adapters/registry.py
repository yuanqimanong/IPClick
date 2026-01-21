from ipclick.adapters.base import DownloaderAdapter
from ipclick.adapters.curl_cffi_adapter import CurlCffiAdapter
from ipclick.adapters.httpx_adapter import HttpxAdapter


ADAPTER_CLASSES = {
    "curl_cffi": CurlCffiAdapter,
    "httpx": HttpxAdapter,
}

# 可选：列表形式（如果只需要顺序列表）
ADAPTER_LIST = list(ADAPTER_CLASSES.values())


def get_default_adapter() -> DownloaderAdapter:
    return ADAPTER_LIST[0]()
