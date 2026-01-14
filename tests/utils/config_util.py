from ipclick.utils.config_util import ConfigUtil
from ipclick.utils.log_util import log

if __name__ == "__main__":
    log.init("debug")

    ConfigUtil.merge([])

    path_1 = r"D:\Projects\IPClick\src\ipclick\configs\default_config.toml"
    path_2 = "test_config.toml"
    # x = ConfigUtil.load(r"D:\Projects\IPClick\src\ipclick\configs\default_config.toml")
    # print(x)
    x = ConfigUtil.load([path_1, path_2])
    print(x.to_json())
