"""浏览器页面并发上限按**内存**推导（0.7.0）。

容易搞反的一点：浏览器页面是**内存**瓶颈，不是 CPU 瓶颈。按核数算的话，
一台 16 核 4GB 的机器会开出 16 个 camoufox 页面（约 6.4GB），直接换页——
而现象是请求从几秒变几分钟、看起来像卡死，很难联想到是并发开太大。
CPU 核数只做上限。
"""

from typing import Any

import pytest

from ipclick.adapters.browser_settings import (
    ENGINE_PAGE_BUDGET_MB,
    MAX_AUTO_PAGES,
    MEMORY_HEADROOM_MB,
    BrowserSettings,
    resolve_max_pages,
)


class TestExplicitConfigWins:
    @pytest.mark.parametrize("configured", [1, 4, 32])
    def test_positive_value_is_used_as_is(self, configured: int, monkeypatch: pytest.MonkeyPatch) -> None:
        """显式配置永远优先——自动推导不该覆盖人的决定，哪怕它看起来不合理。"""
        monkeypatch.setattr("ipclick.adapters.browser_settings.available_memory_mb", lambda: 128)
        assert resolve_max_pages(configured, "camoufox") == configured


class TestMemoryDerivation:
    @staticmethod
    def _with(monkeypatch: pytest.MonkeyPatch, mem_mb: int, cpus: int = 64) -> None:
        monkeypatch.setattr("ipclick.adapters.browser_settings.available_memory_mb", lambda: mem_mb)
        monkeypatch.setattr("ipclick.adapters.browser_settings.os.cpu_count", lambda: cpus)

    def test_camoufox_gets_fewer_pages_than_chromium(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """camoufox 是 Firefox 加一整套扩展，单页开销明显更高，该少开。

        内存要选在**两者都没撞到 MAX_AUTO_PAGES** 的区间，否则两边都被硬上限
        截平、断言恒等，测不出系数差异——这正是第一版写 8192 时踩的坑。
        """
        self._with(monkeypatch, MEMORY_HEADROOM_MB + 2048)
        camoufox = resolve_max_pages(0, "camoufox")
        chromium = resolve_max_pages(0, "playwright")
        assert camoufox < MAX_AUTO_PAGES and chromium < MAX_AUTO_PAGES, "内存选大了，两边都撞上限"
        assert camoufox < chromium

    def test_small_machine_gets_one_page(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """内存吃紧时退到 1，而不是 0（0 会让信号量永远拿不到额度）。"""
        self._with(monkeypatch, MEMORY_HEADROOM_MB + 100)
        assert resolve_max_pages(0, "camoufox") == 1

    def test_never_returns_zero_even_with_no_memory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._with(monkeypatch, 0)
        assert resolve_max_pages(0, "camoufox") == 1

    def test_cpu_count_caps_the_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """内存再多也不该开得比核数还多——页面渲染确实要 CPU。"""
        self._with(monkeypatch, 1_000_000, cpus=2)
        assert resolve_max_pages(0, "playwright") == 2

    def test_hard_ceiling_applies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """内存和核数都极多时仍有硬上限：再多的页面收益会被目标站点吃掉，
        而每个页面都是实打实的内存。"""
        self._with(monkeypatch, 1_000_000, cpus=1024)
        assert resolve_max_pages(0, "playwright") == MAX_AUTO_PAGES

    def test_leaves_headroom_for_the_system(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """把内存吃到一滴不剩比少开几个页面糟得多。"""
        budget = ENGINE_PAGE_BUDGET_MB["playwright"]
        self._with(monkeypatch, MEMORY_HEADROOM_MB + budget * 4)
        assert resolve_max_pages(0, "playwright") == 4

    def test_falls_back_to_static_default_when_memory_is_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """读不出内存就退回静态默认值，别自作聪明。"""
        monkeypatch.setattr("ipclick.adapters.browser_settings.available_memory_mb", lambda: None)
        assert resolve_max_pages(0, "camoufox") == BrowserSettings.max_pages

    def test_unknown_engine_uses_the_generic_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._with(monkeypatch, MEMORY_HEADROOM_MB + 2048)
        assert resolve_max_pages(0, "某个自定义引擎") >= 1


class TestContainerAwareness:
    def test_prefers_the_cgroup_limit_over_host_memory(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """容器里 /proc/meminfo 报的是**宿主机**内存。

        照它推导会在一个 512MB 的容器里开出十几个页面，然后被 OOM killer 杀掉，
        而现象只是"容器莫名其妙重启"——最难往并发上想的一类故障。
        """
        from ipclick.adapters import browser_settings

        limit = tmp_path / "memory.max"
        current = tmp_path / "memory.current"
        limit.write_text(str(512 * 1024 * 1024), encoding="utf-8")
        current.write_text(str(64 * 1024 * 1024), encoding="utf-8")

        class FakePath:
            def __init__(self, p: str) -> None:
                self._p = p

            def read_text(self, encoding: str = "utf-8") -> str:
                mapping = {
                    "/sys/fs/cgroup/memory.max": limit.read_text(),
                    "/sys/fs/cgroup/memory.current": current.read_text(),
                    "/proc/meminfo": "MemAvailable:   99999999 kB\n",
                }
                if self._p in mapping:
                    return mapping[self._p]
                raise OSError(self._p)

        monkeypatch.setattr("pathlib.Path", FakePath)
        available = browser_settings.available_memory_mb()
        assert available is not None
        # 应当取 cgroup 的 (512 - 64) = 448MB，而不是宿主机报的那个天文数字
        assert available == 448, f"没有优先采信 cgroup 限额，拿到了 {available}MB"


class TestMaxPagesWiring:
    """`max_pages = 0` 要真的从**配置文件**走到自动推导。

    这一组存在的理由：本文件原有的用例全都直接调 `resolve_max_pages(0, ...)`，
    函数本身一直是对的，断的是**接线**——`from_config` 用默认 `minimum=1` 解析
    `max_pages`，0 被判成"低于下限"静默回落到静态默认值 4。于是 0.7.0 宣传的
    「按可用内存自动推导」从配置文件根本走不到，而且现象是"配了等于没配"，
    不报错、不打日志。

    单元测过、接线没测过——和 aio 鉴权拦截器那个洞是同一个形状。
    """

    def test_zero_survives_from_config(self) -> None:
        """0 必须原样传下去，不能被当成非法小值吃掉。"""
        assert BrowserSettings.from_config({"max_pages": 0}).max_pages == 0

    def test_zero_reaches_the_auto_path(self) -> None:
        """从配置一路走到最终生效值，结果要和直接调自动推导一致。"""
        settings = BrowserSettings.from_config({"max_pages": 0})
        assert resolve_max_pages(settings.max_pages, "camoufox") == resolve_max_pages(0, "camoufox")

    def test_zero_is_not_the_same_as_unset(self) -> None:
        """配 0 和不配必须是两件事——它们相等就说明自动推导没生效。"""
        configured_zero = BrowserSettings.from_config({"max_pages": 0}).max_pages
        unset = BrowserSettings.from_config({}).max_pages
        assert configured_zero == 0
        assert unset == BrowserSettings.max_pages

    def test_explicit_value_still_wins(self) -> None:
        assert BrowserSettings.from_config({"max_pages": 8}).max_pages == 8

    def test_negative_falls_back_to_default(self) -> None:
        """负数是真的非法（不像 0 有特殊含义），回落到默认值。"""
        assert BrowserSettings.from_config({"max_pages": -3}).max_pages == BrowserSettings.max_pages
