from examples.base_config import HTTPBIN_GET_URL
from ipclick import Downloader
from ipclick.utils.log_util import log


log.init(level="debug")

if __name__ == "__main__":
    x = Downloader()
    x.get(HTTPBIN_GET_URL)
    log.debug("1")
