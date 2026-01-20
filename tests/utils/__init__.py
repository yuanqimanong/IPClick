from examples.base_config import HTTPBIN_GET_URL
from ipclick import Downloader
from ipclick.utils.log_util import log


log.init(level="debug")

if __name__ == "__main__":
    x = Downloader(host="127.0.0.1")
    xx = x.get(HTTPBIN_GET_URL)
    log.warning(xx.text)
    log.debug("1")
