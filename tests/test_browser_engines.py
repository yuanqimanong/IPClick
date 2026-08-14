"""浏览器引擎选择与注册。

引擎的**渲染行为**在 test_browser_adapter.py / test_drission_adapter.py 里验，
这里只管"选哪个引擎、怎么报错"——这部分不需要浏览器，任何环境都能跑。
"""

import asyncio
from pathlib import Path
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


class TestTwoLevelInstallCheck:
    """「Python 包装了」和「浏览器本体下了」是两件事。

    只查前者的后果很具体：``pip install "ipclick[camoufox]"`` 但没跑
    ``camoufox fetch`` 的机器上，config-info 和 Web 端都显示"可用"，而第一个
    浏览器请求会在 gRPC 处理线程里开始下载 1 GB 上下的浏览器本体——请求必然超时，
    并发的首请求还可能各自触发一次下载。
    """

    def test_executable_path_is_definitive(self, tmp_path: Path):
        """显式指定了可执行文件就以它为准：容器里复用系统浏览器的标准做法。"""
        exe = tmp_path / "chromium"
        exe.write_text("#!/bin/sh\n", encoding="utf-8")
        settings = BrowserSettings(executable_path=str(exe))
        for engine in sorted(be.ENGINE_NAMES):
            ready, detail = be.browser_ready(engine, settings)
            assert ready is True, engine
            assert str(exe) in detail

    def test_missing_executable_path_is_reported(self, tmp_path: Path):
        settings = BrowserSettings(executable_path=str(tmp_path / "nope"))
        ready, detail = be.browser_ready("playwright", settings)
        assert ready is False
        assert "不存在" in detail

    def test_playwright_registry_dir_follows_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
        assert be._playwright_registry_dir("playwright") == tmp_path  # pyright: ignore[reportPrivateUsage]

    def test_playwright_finds_browser_in_flat_layout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
        (tmp_path / "chromium-1148").mkdir()
        ready, detail = be.browser_ready("playwright", BrowserSettings(kind="chromium"))
        assert ready is True
        assert "chromium-1148" in detail

    def test_playwright_finds_browser_in_nested_layout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """新版 playwright 多了一层 ``ms-playwright/b/``，写死一层会漏。"""
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
        (tmp_path / "b" / "chromium-1200").mkdir(parents=True)
        assert be.browser_ready("playwright", BrowserSettings(kind="chromium"))[0] is True

    def test_playwright_missing_registry_dir_is_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "absent"))
        ready, detail = be.browser_ready("playwright", BrowserSettings())
        assert ready is False
        assert "install" in detail

    def test_playwright_wrong_kind_is_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
        (tmp_path / "chromium-1148").mkdir()
        ready, _ = be.browser_ready("playwright", BrowserSettings(kind="webkit"))
        assert ready is False

    def test_status_label_distinguishes_the_three_states(self):
        from ipclick.adapters.browser_engines import EngineStatus

        assert EngineStatus("x", package=False, browser=None).label == "包未安装"
        assert EngineStatus("x", package=True, browser=False).label == "包已装，浏览器本体未就绪"
        assert EngineStatus("x", package=True, browser=None).label == "包已装，本体未知"
        assert EngineStatus("x", package=True, browser=True).label == "可用"

    def test_unknown_browser_state_counts_as_ready(self):
        """查不出来时按能用处理：宁可让它真启动一次去报错，
        也不要因为检查不到就拒绝一台其实装好了的机器。"""
        from ipclick.adapters.browser_engines import EngineStatus

        assert EngineStatus("x", package=True, browser=None).ready is True
        assert EngineStatus("x", package=True, browser=False).ready is False
        assert EngineStatus("x", package=False, browser=True).ready is False

    def test_missing_package_short_circuits(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(be, "_playwright_api", None)
        status = be.engine_status("playwright")
        assert status.package is False
        assert status.ready is False
        assert "pip install" in status.detail

    def test_registry_registers_on_package_not_readiness(self):
        """本体没下时适配器仍要注册，否则报错会变成"尚未支持"，指错方向。"""
        import inspect

        from ipclick.adapters import registry

        source = inspect.getsource(registry)
        assert "package_installed" in source
        assert "if be.is_available(_engine)" not in source


class TestNoSilentDownload:
    def test_launch_refuses_when_browser_body_missing(self, monkeypatch: pytest.MonkeyPatch):
        """camoufox 缺本体时的默认行为是当场下载 1 GB+。
        这里必须在进 camoufox 之前就拦住——那时已经在 gRPC 请求线程上了。
        """
        import asyncio

        from ipclick.exceptions import AdapterError

        monkeypatch.setattr(
            be,
            "engine_status",
            lambda engine, settings=None: be.EngineStatus(
                engine, package=True, browser=False, detail="未下载（CamoufoxNotInstalled）"
            ),
        )
        called: list[str] = []
        monkeypatch.setattr(be, "_launch_camoufox", lambda s: called.append("launched"))

        with pytest.raises(AdapterError, match=r"未就绪|未下载"):
            asyncio.run(be.launch("camoufox", BrowserSettings()))
        assert called == [], "拦不住就意味着会去下载"


class TestCamoufoxNeverDownloadsOnDemand:
    """camoufox 缺本体时的默认行为是当场下载（本机实测 2 分钟下了 440 MB）。

    那一刻已经在 gRPC 请求线程上：请求必然超时，超时返回后下载还在后台跑，
    看起来就像"这个引擎坏了"。所以必须在结构上排除这条路，而不是只加检查。
    """

    def test_executable_path_is_always_passed(self, monkeypatch: pytest.MonkeyPatch):
        """核心回归：不传 executable_path 就等于把路径解析权交给 camoufox，
        而它的解析器缺本体时会去下载。
        """
        captured: dict[str, object] = {}

        async def fake_new_browser(_driver: object, **kwargs: object):
            captured.update(kwargs)
            return object()

        class FakeDriver:
            async def stop(self) -> None: ...

        async def fake_start():
            return FakeDriver()

        monkeypatch.setattr(be, "_camoufox_new_browser", fake_new_browser)
        monkeypatch.setattr(be, "_playwright_api", lambda: type("A", (), {"start": staticmethod(fake_start)})())
        monkeypatch.setattr(be, "_camoufox_executable", lambda: "/resolved/by/us/camoufox-bin")

        asyncio.run(be._launch_camoufox(BrowserSettings(engine="camoufox")))  # pyright: ignore[reportPrivateUsage]
        assert captured.get("executable_path") == "/resolved/by/us/camoufox-bin"

    def test_configured_executable_path_wins(self, monkeypatch: pytest.MonkeyPatch):
        captured: dict[str, object] = {}

        async def fake_new_browser(_driver: object, **kwargs: object):
            captured.update(kwargs)
            return object()

        class FakeDriver:
            async def stop(self) -> None: ...

        monkeypatch.setattr(be, "_camoufox_new_browser", fake_new_browser)
        monkeypatch.setattr(
            be, "_playwright_api", lambda: type("A", (), {"start": staticmethod(lambda: _done(FakeDriver()))})()
        )

        def boom() -> str:
            raise AssertionError("配了 executable_path 就不该再去解析 camoufox 的安装目录")

        monkeypatch.setattr(be, "_camoufox_executable", boom)
        settings = BrowserSettings(engine="camoufox", executable_path="/opt/my/camoufox")
        asyncio.run(be._launch_camoufox(settings))  # pyright: ignore[reportPrivateUsage]
        assert captured.get("executable_path") == "/opt/my/camoufox"

    def test_missing_body_raises_with_the_fetch_command(self, monkeypatch: pytest.MonkeyPatch):
        import camoufox.pkgman

        def not_installed(*_args: object, **_kwargs: object):
            raise RuntimeError("official is not installed. Please run `camoufox fetch` to install.")

        monkeypatch.setattr(camoufox.pkgman, "camoufox_path", not_installed)
        with pytest.raises(AdapterError, match="camoufox fetch"):
            _ = be._camoufox_executable()  # pyright: ignore[reportPrivateUsage]

    def test_resolver_never_asks_camoufox_to_download(self, monkeypatch: pytest.MonkeyPatch):
        """核心回归：camoufox 的 ``camoufox_path`` / ``launch_path`` 默认
        ``download_if_missing=True`` —— 连"查一下装没装"都会开始下 1 GB。
        Web 端渲染一次总览页就会触发，所以必须显式传 False。
        """
        import camoufox.pkgman

        seen: list[object] = []

        def spy(download_if_missing: bool = True, **_kwargs: object):
            seen.append(download_if_missing)
            raise RuntimeError("not installed")

        monkeypatch.setattr(camoufox.pkgman, "camoufox_path", spy)
        ready, detail = be._camoufox_browser_ready()  # pyright: ignore[reportPrivateUsage]
        assert seen == [False], f"必须显式传 download_if_missing=False，实际 {seen}"
        assert ready is False
        assert "camoufox fetch" in detail

    def test_status_check_does_not_call_launch_path(self):
        """护栏：``launch_path()`` 内部走 download_if_missing=True，调它就会下载。

        用 AST 找真实的函数调用，而不是按文本搜——文档串里提到这个名字是在解释
        "为什么不用它"，按文本搜会把说明当成违规。
        """
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(be))
        called = {
            node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "launch_path" not in called, "launch_path() 会触发下载，请用 camoufox_path(download_if_missing=False)"


async def _done(value: object) -> object:
    return value


class TestBrowserRecovery:
    """浏览器进程死掉之后要能重建。

    小内存机器上 chromium 被 OOM killer 干掉是常事（日志里会出现
    "Network service crashed"）。以前 _ensure_browser 只判 is not None，
    于是这个节点之后每个浏览器请求都必败，运维现象是"重启进程才好"。
    """

    def _worker(self, monkeypatch: pytest.MonkeyPatch, browsers: list[object]):
        from ipclick.adapters.browser_adapter import _BrowserWorker

        launched = iter(browsers)

        async def fake_launch(engine: str, settings: object):
            from ipclick.adapters.browser_engines import LaunchedBrowser

            class Driver:
                async def stop(self) -> None: ...

            return LaunchedBrowser(driver=Driver(), browser=next(launched))

        monkeypatch.setattr(be, "launch", fake_launch)
        return _BrowserWorker(BrowserSettings(engine="playwright"), "playwright")

    def test_dead_browser_is_replaced(self, monkeypatch: pytest.MonkeyPatch):
        class Browser:
            def __init__(self, alive: bool) -> None:
                self._alive = alive

            def is_connected(self) -> bool:
                return self._alive

            async def close(self) -> None:
                self._alive = False

        dead, fresh = Browser(alive=False), Browser(alive=True)
        worker = self._worker(monkeypatch, [dead, fresh])
        loop = asyncio.new_event_loop()
        try:
            first = loop.run_until_complete(worker._ensure_browser())  # pyright: ignore[reportPrivateUsage]
            assert first is dead
            second = loop.run_until_complete(worker._ensure_browser())  # pyright: ignore[reportPrivateUsage]
            assert second is fresh, "失联的浏览器必须被换掉"
        finally:
            loop.close()

    def test_live_browser_is_reused(self, monkeypatch: pytest.MonkeyPatch):
        """别把复用弄没了——每次请求重启浏览器比不重建还糟。"""

        class Browser:
            def is_connected(self) -> bool:
                return True

        only = Browser()
        worker = self._worker(monkeypatch, [only])
        loop = asyncio.new_event_loop()
        try:
            a = loop.run_until_complete(worker._ensure_browser())  # pyright: ignore[reportPrivateUsage]
            b = loop.run_until_complete(worker._ensure_browser())  # pyright: ignore[reportPrivateUsage]
            assert a is b is only
        finally:
            loop.close()

    def test_unprobeable_browser_counts_as_alive(self, monkeypatch: pytest.MonkeyPatch):
        """探测不出来时按活着处理：误判成死掉会白白重启一个好浏览器。"""

        class Browser:
            pass

        only = Browser()
        worker = self._worker(monkeypatch, [only])
        loop = asyncio.new_event_loop()
        try:
            a = loop.run_until_complete(worker._ensure_browser())  # pyright: ignore[reportPrivateUsage]
            b = loop.run_until_complete(worker._ensure_browser())  # pyright: ignore[reportPrivateUsage]
            assert a is b
        finally:
            loop.close()


class TestPermanentNavigationErrors:
    """浏览器说"这个 URL 我压根不会去连"时，重试没有意义。

    实测：一个 ERR_UNSAFE_PORT 的 URL 要 15.9 秒、重试 4 次才返回，
    而且报成 -1，让调用方以为是网络故障。
    """

    @pytest.mark.parametrize(
        "message",
        [
            "Page.goto: net::ERR_UNSAFE_PORT at http://127.0.0.1:1/",
            "net::ERR_UNKNOWN_URL_SCHEME",
            "net::ERR_INVALID_URL",
        ],
    )
    def test_permanent_errors_become_validation_errors(self, message: str):
        from ipclick.adapters.base import raise_if_permanent_navigation_error
        from ipclick.exceptions import ValidationError

        with pytest.raises(ValidationError, match="浏览器拒绝访问"):
            raise_if_permanent_navigation_error(RuntimeError(message))

    @pytest.mark.parametrize(
        "message",
        [
            "net::ERR_CONNECTION_REFUSED",
            "net::ERR_TIMED_OUT",
            "net::ERR_NETWORK_CHANGED",
            "Timeout 30000ms exceeded",
        ],
    )
    def test_transient_errors_are_left_alone(self, message: str):
        """别把重试整个关掉——真的网络抖动仍然要重试。"""
        from ipclick.adapters.base import raise_if_permanent_navigation_error

        raise_if_permanent_navigation_error(RuntimeError(message))
