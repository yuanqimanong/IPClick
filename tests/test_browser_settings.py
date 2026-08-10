"""[BROWSER] 配置解析。

这一节此前完全没有消费方，配了也不生效。现在它驱动 playwright 适配器，
所以每个键都得真的被读到、非法值得有确定的回落行为。
"""

from ipclick.adapters.browser_settings import BrowserSettings


class TestDefaults:
    def test_empty_config_uses_defaults(self):
        s = BrowserSettings.from_config({})
        assert (s.kind, s.headless, s.enabled) == ("chromium", True, True)
        assert s.executable_path is None
        assert s.viewport == {"width": 1920, "height": 1080}

    def test_none_config_uses_defaults(self):
        assert BrowserSettings.from_config(None).kind == "chromium"

    def test_scripts_disabled_by_default(self):
        """页面内 JS 能绕过 URL 策略访问内网，必须默认关闭。"""
        assert BrowserSettings().allow_scripts is False
        assert BrowserSettings.from_config({}).allow_scripts is False

    def test_sandbox_on_by_default(self):
        """--no-sandbox 削弱进程隔离，得由部署方明确选择。"""
        assert BrowserSettings.from_config({}).no_sandbox is False


class TestParsing:
    def test_full_config(self):
        s = BrowserSettings.from_config(
            {
                "enabled": True,
                "browser": "firefox",
                "headless": False,
                "executable_path": "/usr/bin/chromium",
                "args": ["--foo", "--bar"],
                "no_sandbox": True,
                "user_agent": "UA/1.0",
                "viewport": {"width": 800, "height": 600},
                "wait_until": "networkidle",
                "block_resources": ["image", "script"],
                "max_pages": 2,
                "allow_scripts": True,
                "proxy": {"gateway": "http://127.0.0.1:8080", "bypass_list": ["*.internal.com"]},
                "timeout": {"page_load": 15, "script_exec": 5},
            }
        )
        assert s.kind == "firefox"
        assert s.headless is False
        assert s.executable_path == "/usr/bin/chromium"
        assert s.args == ("--foo", "--bar")
        assert s.no_sandbox is True
        assert s.user_agent == "UA/1.0"
        assert s.viewport == {"width": 800, "height": 600}
        assert s.wait_until == "networkidle"
        assert s.block_resources == ("image", "script")
        assert s.max_pages == 2
        assert s.allow_scripts is True
        assert s.proxy_gateway == "http://127.0.0.1:8080"
        assert s.proxy_bypass == ("*.internal.com",)
        assert (s.page_load_timeout, s.script_timeout) == (15.0, 5.0)

    def test_empty_strings_become_none(self):
        """默认配置里这几项是空串占位，不能被当成"真的配了个空路径"。"""
        s = BrowserSettings.from_config(
            {"executable_path": "", "user_agent": "  ", "proxy": {"gateway": ""}},
        )
        assert (s.executable_path, s.user_agent, s.proxy_gateway) == (None, None, None)

    def test_empty_block_resources_means_block_nothing(self):
        """显式配空列表是"什么都不拦"，不该被回落成默认的三项。"""
        assert BrowserSettings.from_config({"block_resources": []}).block_resources == ()

    def test_omitted_block_resources_keeps_default(self):
        assert BrowserSettings.from_config({}).block_resources == ("image", "media", "font")


class TestInvalidValues:
    def test_unknown_browser_falls_back(self):
        assert BrowserSettings.from_config({"browser": "netscape"}).kind == "chromium"

    def test_browser_is_case_insensitive(self):
        assert BrowserSettings.from_config({"browser": "WebKit"}).kind == "webkit"

    def test_unknown_wait_until_falls_back(self):
        assert BrowserSettings.from_config({"wait_until": "whenever"}).wait_until == "load"

    def test_unknown_resource_types_dropped(self):
        s = BrowserSettings.from_config({"block_resources": ["image", "hologram"]})
        assert s.block_resources == ("image",)

    def test_non_list_block_resources_ignored(self):
        assert BrowserSettings.from_config({"block_resources": "image"}).block_resources == ()

    def test_bad_numbers_fall_back(self):
        s = BrowserSettings.from_config(
            {"max_pages": "abc", "viewport": {"width": 0}, "timeout": {"page_load": -5}},
        )
        assert s.max_pages == 4
        assert s.viewport["width"] == 1920
        assert s.page_load_timeout == 30.0

    def test_max_pages_floor_is_one(self):
        """0 会让信号量永远拿不到额度，请求全部卡死。"""
        assert BrowserSettings.from_config({"max_pages": 0}).max_pages == 4
