from ipclick.utils.config_util import ConfigUtil

if __name__ == "__main__":
    path_1 = r"D:\Projects\IPClick\src\ipclick\configs\default_config.toml"
    path_2 = "test_config.toml"
    # x = ConfigUtil.load(r"D:\Projects\IPClick\src\ipclick\configs\default_config.toml")
    # print(x)
    x = ConfigUtil.load([path_1, path_2])
    print(x.to_json())
