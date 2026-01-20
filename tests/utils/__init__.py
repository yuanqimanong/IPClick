from examples.base_config import HTTPBIN_GET_URL
from ipclick import Downloader
from ipclick.utils.log_util import log


log.init(level="debug")

if __name__ == "__main__":
    x = Downloader()
    xx = x.get('http://httpbin.org/ip',proxy=True)
    log.warning(xx.text)
    log.debug("1")
