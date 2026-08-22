from __future__ import annotations

from enum import Enum
from pathlib import Path
import sys
from types import ModuleType

import pytest

from ipclick.adapters import browser_engines
from ipclick.adapters.browser_adapter import NETWORK_IDLE, _goto_state, _RenderPlan, _settle
from ipclick.adapters.browser_settings import BrowserSettings


class FakePage:
    def __init__(self, fail: bool = False) -> None:
        self.states: list[tuple[str, int]] = []
        self.fail: bool = fail

    async def wait_for_load_state(self, state: str, timeout: int) -> None:
        self.states.append((state, timeout))
        if self.fail:
            raise TimeoutError("no idle window")


def _plan(wait_until: str, *, settle_ms: int = 5000, page_ms: int = 30000) -> _RenderPlan:
    return _RenderPlan(
        url="http://127.0.0.1/x",
        context_options={},
        cookies=[],
        block_resources=(),
        wait_until=wait_until,
        page_timeout_ms=page_ms,
        script_timeout=60.0,
        settle_timeout_ms=settle_ms,
    )


def test_default_wait_until_no_longer_silently_drops_late_content() -> None:
    assert BrowserSettings().wait_until == NETWORK_IDLE
    assert BrowserSettings().settle_timeout == 5.0


def test_settle_timeout_is_configurable() -> None:
    settings = BrowserSettings.from_config({"timeout": {"settle": 2.5}})
    assert settings.settle_timeout == 2.5
    assert BrowserSettings.from_config({"timeout": {"settle": 0}}).settle_timeout == 0.0
    assert BrowserSettings.from_config({"timeout": {"settle": "nonsense"}}).settle_timeout == 5.0


@pytest.mark.parametrize(
    ("wait_until", "expected"),
    [("networkidle", "load"), ("load", "load"), ("domcontentloaded", "domcontentloaded"), ("commit", "commit")],
)
def test_navigation_never_waits_for_idle_directly(wait_until: str, expected: str) -> None:
    assert _goto_state(wait_until) == expected


async def test_idle_is_awaited_after_the_load_event() -> None:
    page = FakePage()
    await _settle(page, _plan(NETWORK_IDLE))
    assert page.states == [(NETWORK_IDLE, 5000)]


async def test_idle_budget_never_exceeds_the_page_timeout() -> None:
    page = FakePage()
    await _settle(page, _plan(NETWORK_IDLE, settle_ms=30000, page_ms=4000))
    assert page.states == [(NETWORK_IDLE, 4000)]


async def test_a_page_that_never_goes_idle_still_returns_content() -> None:
    page = FakePage(fail=True)
    await _settle(page, _plan(NETWORK_IDLE))
    assert page.states == [(NETWORK_IDLE, 5000)]


@pytest.mark.parametrize(("wait_until", "settle_ms"), [("load", 5000), (NETWORK_IDLE, 0)])
async def test_settle_is_skipped_when_not_asked_for(wait_until: str, settle_ms: int) -> None:
    page = FakePage()
    await _settle(page, _plan(wait_until, settle_ms=settle_ms))
    assert page.states == []


def test_unknown_wait_until_falls_back_to_the_default() -> None:
    assert BrowserSettings.from_config({"wait_until": "whenever"}).wait_until == NETWORK_IDLE
    assert BrowserSettings.from_config({"wait_until": " LOAD "}).wait_until == "load"


def _fake_addons_module(addons_dir: Path, names: tuple[str, ...]) -> ModuleType:
    """伪造 camoufox.addons：只要 ADDONS_DIR 和 DefaultAddons 两个符号。"""
    module = ModuleType("camoufox.addons")
    module.ADDONS_DIR = addons_dir  # pyright: ignore[reportAttributeAccessIssue]
    module.DefaultAddons = Enum("DefaultAddons", {n: n for n in names})  # pyright: ignore[reportAttributeAccessIssue]
    return module


def test_an_empty_addon_directory_is_reported_as_not_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ "目录在、内容是空的"必须判成未就绪，不能报"就绪"。

    camoufox 的 maybe_download_addons() 只看插件目录存不存在、不看内容，空目录照样被
    当成有效插件塞进启动参数，直到 confirm_paths() 才抛 InvalidAddonPath。而这种空目录
    很容易留下：`python -m camoufox fetch` 下插件失败时（addons.mozilla.org 会按地区
    返回 451）先 makedirs 再下载，异常被它自己吞掉，退出码仍是 0。
    只看浏览器二进制的话，就会对一个**必然启动失败**的引擎回答"就绪"。
    """
    addons = tmp_path / "addons"
    (addons / "UBO").mkdir(parents=True)
    monkeypatch.setitem(sys.modules, "camoufox.addons", _fake_addons_module(addons, ("UBO",)))

    assert browser_engines._broken_camoufox_addons() == str(addons / "UBO")


def test_a_complete_addon_directory_is_fine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    addons = tmp_path / "addons"
    (addons / "UBO").mkdir(parents=True)
    (addons / "UBO" / "manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "camoufox.addons", _fake_addons_module(addons, ("UBO",)))

    assert browser_engines._broken_camoufox_addons() == ""


def test_a_missing_addon_directory_is_not_treated_as_broken(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """目录整个不存在是另一回事：camoufox 启动时会尝试补下，失败也只是跳过该插件，能起来。

    Dockerfile 的 --build-arg CAMOUFOX_REQUIRE_ADDONS=0 走的正是这条路——把空目录删掉，
    换来一个稳定可用、只是不带插件的镜像。所以这里绝不能把"没有"也判成坏。
    """
    addons = tmp_path / "addons"
    addons.mkdir()
    monkeypatch.setitem(sys.modules, "camoufox.addons", _fake_addons_module(addons, ("UBO",)))

    assert browser_engines._broken_camoufox_addons() == ""


def test_both_browser_adapters_share_the_automation_config_helpers() -> None:
    """两个浏览器适配器不能各存一份 automation_config 解析与等待钳位。

    原来 _parse_automation_config 在两边逐字重复，wait_for_timeout 的钳位一边写成
    _positive_number + 调用点 min()、另一边写成 _wait_timeout_ms，上限常量
    60_000 也各定义了一份。这份复制已经付过代价：_scroll_to_bottom 的
    document.body 判空只补在了 playwright 那边，DrissionPage 那边漏了。
    """
    from ipclick.adapters.browser_adapter import BrowserAdapter
    from ipclick.adapters.drission_adapter import DrissionPageAdapter

    for adapter_cls in (BrowserAdapter, DrissionPageAdapter):
        for gone in ("_parse_automation_config", "_positive_number", "_wait_timeout_ms"):
            assert gone not in vars(adapter_cls), f"{adapter_cls.__name__} 又自己实现了 {gone}"


def test_the_scroll_helpers_guard_document_body() -> None:
    """两个引擎用的是同一份页内语句，判空不能只补一边。"""
    from ipclick.adapters.browser_settings import DOCUMENT_HEIGHT_JS, SCROLL_TO_BOTTOM_JS

    assert "document.body ?" in DOCUMENT_HEIGHT_JS
    assert SCROLL_TO_BOTTOM_JS.startswith("if (document.body)")
