"""浏览器引擎选择与注册。

引擎的**渲染行为**在 test_browser_adapter.py / test_drission_adapter.py 里验，
这里只管"选哪个引擎、怎么报错"——这部分不需要浏览器，任何环境都能跑。
"""

import sys

import pytest

from ipclick.adapters import browser_engines as be
from ipclick.adapters.browser_adapter import CamoufoxAdapter, PatchrightAdapter, PlaywrightAdapter
from ipclick.adapters.browser_settings import BrowserSettings
from ipclick.adapters.drission_adapter import DrissionPageAdapter
from ipclick.adapters.registry import (
    GENERIC_BROWSER_NAME,
    get_adapter,
    resolve_browser_adapter_name,
)
from ipclick.exceptions import AdapterError, ConfigError


class TestPlatformDefault:
    def test_windows_uses_drissionpage(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(sys, "platform", "win32")
        assert be.default_engine() == "drissionpage"

    def test_linux_uses_camoufox(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert be.default_engine() == "camoufox"

    def test_macos_uses_camoufox(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        assert be.default_engine() == "camoufox"


class TestResolveEngine:
    def test_auto_follows_platform(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(sys, "platform", "win32")
        assert be.resolve_engine("auto") == "drissionpage"
        monkeypatch.setattr(sys, "platform", "linux")
        assert be.resolve_engine("auto") == "camoufox"

    def test_empty_and_none_mean_auto(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert be.resolve_engine(None) == "camoufox"
        assert be.resolve_engine("") == "camoufox"

    def test_explicit_wins(self):
        assert be.resolve_engine("patchright") == "patchright"
        assert be.resolve_engine("PlayWright") == "playwright"

    def test_unknown_engine_raises(self):
        """配置写错了要让人知道。静默回退到默认引擎，用户会以为反检测生效了，
        实际用的是原版 playwright。"""
        with pytest.raises(ConfigError, match="未知的浏览器引擎"):
            be.resolve_engine("netscape")

    def test_error_lists_valid_choices(self):
        with pytest.raises(ConfigError, match="camoufox"):
            be.resolve_engine("nope")


class TestGenericBrowserAdapter:
    """客户端说"用浏览器渲染就行"，服务端决定用哪个引擎。"""

    def test_resolves_by_config(self):
        assert resolve_browser_adapter_name(BrowserSettings(engine="patchright")) == "patchright"
        assert resolve_browser_adapter_name(BrowserSettings(engine="camoufox")) == "camoufox"
        assert resolve_browser_adapter_name(BrowserSettings(engine="drissionpage")) == "DrissionPage"

    def test_auto_resolves_by_platform(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(sys, "platform", "win32")
        assert resolve_browser_adapter_name(BrowserSettings(engine="auto")) == "DrissionPage"
        monkeypatch.setattr(sys, "platform", "darwin")
        assert resolve_browser_adapter_name(BrowserSettings(engine="auto")) == "camoufox"

    def test_none_settings_still_resolves(self):
        assert resolve_browser_adapter_name(None) in {"camoufox", "DrissionPage"}

    def test_get_adapter_honours_generic_name(self):
        settings = BrowserSettings(engine="playwright", executable_path="/usr/bin/chromium", no_sandbox=True)
        adapter = get_adapter(GENERIC_BROWSER_NAME, None, settings)
        try:
            assert isinstance(adapter, PlaywrightAdapter)
            assert adapter.resolved_engine == "playwright"
        finally:
            adapter.close()

    def test_bad_engine_surfaces_as_config_error(self):
        """引擎名写错是配置问题，不该被报成"适配器不存在"。"""
        with pytest.raises(ConfigError, match="未知的浏览器引擎"):
            get_adapter(GENERIC_BROWSER_NAME, None, BrowserSettings(engine="netscape"))


class TestEngineAdapters:
    @pytest.mark.parametrize(
        ("cls", "name", "engine"),
        [
            (PlaywrightAdapter, "playwright", "playwright"),
            (PatchrightAdapter, "patchright", "patchright"),
            (CamoufoxAdapter, "camoufox", "camoufox"),
            (DrissionPageAdapter, "DrissionPage", "drissionpage"),
        ],
    )
    def test_names_and_engines(self, cls: type, name: str, engine: str):
        assert cls.adapter_name == name
        if cls is not DrissionPageAdapter:
            assert cls.engine == engine

    def test_pinned_engine_ignores_config(self):
        """点名 playwright 就得是 playwright，不能被 [BROWSER].engine 改掉——
        否则客户端指定引擎这件事就没意义了。"""
        settings = BrowserSettings(engine="camoufox", executable_path="/usr/bin/chromium", no_sandbox=True)
        adapter = PlaywrightAdapter(browser_settings=settings)
        try:
            assert adapter.resolved_engine == "playwright"
        finally:
            adapter.close()

    def test_disabled_blocks_every_engine(self):
        for cls in (PlaywrightAdapter, PatchrightAdapter, CamoufoxAdapter, DrissionPageAdapter):
            with pytest.raises(AdapterError, match="enabled = false"):
                cls(browser_settings=BrowserSettings(enabled=False))

    def test_unavailable_engine_gives_install_hint(self, monkeypatch: pytest.MonkeyPatch):
        """ "没装"和"不支持"要分清楚，前者一条 pip 命令能解决。"""
        monkeypatch.setattr(be, "_patchright_api", None)
        with pytest.raises(AdapterError, match="patchright install chromium"):
            PatchrightAdapter(browser_settings=BrowserSettings())


class TestInstallHints:
    def test_every_engine_has_a_hint(self):
        assert set(be.INSTALL_HINTS) == be.ENGINE_NAMES

    def test_hints_mention_the_extra_and_the_binary(self):
        """光 pip install 还不够，都还要再下一次浏览器——提示里不说，人一定会卡住。"""
        assert "camoufox fetch" in be.INSTALL_HINTS["camoufox"]
        assert "patchright install" in be.INSTALL_HINTS["patchright"]
        assert "playwright install" in be.INSTALL_HINTS["playwright"]
        assert "Chrome" in be.INSTALL_HINTS["drissionpage"]


class TestFingerprintManaged:
    def test_camoufox_is_fingerprint_managed(self):
        assert "camoufox" in be.FINGERPRINT_MANAGED

    def test_plain_playwright_is_not(self):
        assert "playwright" not in be.FINGERPRINT_MANAGED

    def test_managed_engines_skip_viewport_and_ua(self):
        """camoufox 自己生成一整套自洽指纹。再盖一层 viewport / UA 只会自相矛盾，
        反而比不伪装更容易被认出来。"""
        settings = BrowserSettings(engine="camoufox", user_agent="Custom/1.0")
        adapter = CamoufoxAdapter(browser_settings=settings)
        try:
            plan = adapter._build_plan(
                "http://example.com",
                headers=None,
                cookies=None,
                params=None,
                proxy=None,
                timeout=10,
                verify=True,
                automation_config=None,
                automation_script=None,
            )
            assert "viewport" not in plan.context_options
            assert "user_agent" not in plan.context_options
        finally:
            adapter.close()

    def test_unmanaged_engines_still_set_them(self):
        settings = BrowserSettings(engine="playwright", user_agent="Custom/1.0")
        adapter = PlaywrightAdapter(browser_settings=settings)
        try:
            plan = adapter._build_plan(
                "http://example.com",
                headers=None,
                cookies=None,
                params=None,
                proxy=None,
                timeout=10,
                verify=True,
                automation_config=None,
                automation_script=None,
            )
            assert plan.context_options["user_agent"] == "Custom/1.0"
            assert plan.context_options["viewport"] == {"width": 1920, "height": 1080}
        finally:
            adapter.close()


class TestCamoufoxSettings:
    def test_camoufox_only_keys_parsed(self):
        s = BrowserSettings.from_config({"locale": "zh-CN", "humanize": 1.5, "geoip": True})
        assert s.locale == "zh-CN"
        assert s.humanize == 1.5
        assert s.geoip is True

    def test_humanize_accepts_bool(self):
        assert BrowserSettings.from_config({"humanize": True}).humanize is True
        assert BrowserSettings.from_config({"humanize": False}).humanize is False

    def test_humanize_zero_means_off(self):
        assert BrowserSettings.from_config({"humanize": 0}).humanize is False

    def test_defaults_are_off(self):
        s = BrowserSettings.from_config({})
        assert (s.locale, s.humanize, s.geoip) == (None, False, False)

    def test_engine_defaults_to_auto(self):
        assert BrowserSettings.from_config({}).engine == "auto"

    def test_engine_is_normalised(self):
        assert BrowserSettings.from_config({"engine": " CamouFox "}).engine == "camoufox"
