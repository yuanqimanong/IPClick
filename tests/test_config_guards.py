"""配置值写错时，是吵闹地失败还是安静地做错事。

这个文件守的是一类缺陷：**解析函数把非法值静默换成默认值**。它不报错、不打
日志，服务照常起来，只是行为和配置文件上写的不一样。排查时人会盯着配置文件
念"我明明写了啊"，而真相在解析函数里。

0.6.0 已经栽过一次：`processes = false` 经 `int(False)` 变成 0，而 0 的语义是
"按 CPU 核数自动"，于是"别开多进程"被读成"开 8 个进程"。0.7.0 发版自检又在
三个地方发现同一形状的问题，这里逐个钉住。
"""

import pytest

from ipclick.exceptions import ConfigError
from ipclick.limiter import LimiterSettings
from ipclick.server import _as_strict_bool
from ipclick.utils.log_util import LogUtil


class TestStrictBool:
    """`async_mode = "false"` 不能把异步模式打开。

    TOML 里给布尔值加引号是最常见的笔误之一，而 `bool("false")` 是 True。
    原先 `[SERVER].async_mode` 直接 `bool()` 原始值，后果是配置文件上白纸黑字
    写着关、跑起来却是开的实验性模式，且没有任何提示——人会在一个自以为没开的
    模式上排查问题。
    """

    @pytest.mark.parametrize("raw", ["false", "False", "FALSE", "off", "no", "0", ""])
    def test_falsey_strings_are_false(self, raw: str) -> None:
        assert _as_strict_bool(raw, "async_mode") is False

    @pytest.mark.parametrize("raw", ["true", "True", "on", "yes", "1"])
    def test_truthy_strings_are_true(self, raw: str) -> None:
        assert _as_strict_bool(raw, "async_mode") is True

    def test_real_booleans_pass_through(self) -> None:
        assert _as_strict_bool(True, "async_mode") is True
        assert _as_strict_bool(False, "async_mode") is False

    def test_absent_uses_the_default(self) -> None:
        assert _as_strict_bool(None, "async_mode") is False
        assert _as_strict_bool(None, "async_mode", default=True) is True

    @pytest.mark.parametrize("raw", ["maybe", "enabled", 3.5, [], {}])
    def test_ambiguous_values_raise(self, raw: object) -> None:
        """含糊的值必须报错。猜一个出来就是把决定权从人手里拿走。"""
        with pytest.raises(ConfigError, match="async_mode"):
            _as_strict_bool(raw, "async_mode")

    def test_the_error_names_the_field_and_the_value(self) -> None:
        with pytest.raises(ConfigError) as e:
            _as_strict_bool("maybe", "async_mode")
        assert "async_mode" in str(e.value) and "maybe" in str(e.value)


class TestLimiterFailsLoudly:
    """限流是保护性开关，配错了不能安静地变成"不限速"。

    原先 `per_host_qps = "abc"` 和 `-5` 都回落到 0.0，而 0.0 的语义恰好是
    "不限速"。于是配置写错的后果是限流被彻底关掉，日志里一个字都没有：你以为
    自己配了 100 QPS，实际在满速锤目标站点，直到对面返 429 或封 IP 才发现。

    对保护性开关来说这是最坏的失败方向——必须 fail-closed 或吵闹地失败，
    不能安静地把闸门打开。
    """

    def test_valid_value_works(self) -> None:
        s = LimiterSettings.from_config({"rate_limit": {"per_host_qps": 100}})
        assert s.per_host_qps == 100.0
        assert s.enabled

    def test_numeric_string_is_accepted(self) -> None:
        """从环境变量注入配置时值本来就是字符串，能明确判读的照收。"""
        assert LimiterSettings.from_config({"rate_limit": {"per_host_qps": "100"}}).per_host_qps == 100.0

    def test_absent_means_unlimited_and_that_is_fine(self) -> None:
        """没配是"没配"，不是"配错了"——仍然走默认值（不限速）。"""
        s = LimiterSettings.from_config({})
        assert s.per_host_qps == 0.0
        assert not s.enabled

    def test_garbage_raises_instead_of_disabling_the_limiter(self) -> None:
        with pytest.raises(ConfigError, match="per_host_qps"):
            LimiterSettings.from_config({"rate_limit": {"per_host_qps": "abc"}})

    def test_negative_raises(self) -> None:
        with pytest.raises(ConfigError, match="不能小于"):
            LimiterSettings.from_config({"rate_limit": {"per_host_qps": -5}})

    def test_boolean_raises(self) -> None:
        """`float(True)` 是 1.0——会变成"每秒 1 个请求"，一个没人想要的值。"""
        with pytest.raises(ConfigError, match="布尔"):
            LimiterSettings.from_config({"rate_limit": {"per_host_qps": True}})

    @pytest.mark.parametrize(
        "section,key",
        [
            ("concurrency", "per_host_max_concurrent"),
            ("rate_limit", "per_host_burst"),
            ("concurrency", "per_host_wait_timeout"),
            ("concurrency", "per_host_idle_ttl"),
            ("concurrency", "max_tracked_hosts"),
        ],
    )
    def test_every_limiter_field_is_guarded(self, section: str, key: str) -> None:
        """不只是 per_host_qps —— 整组都不能静默吞掉错值。"""
        with pytest.raises(ConfigError, match=key):
            LimiterSettings.from_config({section: {key: "abc"}})


class TestLogLevelAliases:
    """`level = "warn"` 不能让服务起不来。

    配置模板一度把合法值写成 `(debug/info/warn/error)`，而底层 loguru 只认
    WARNING。照着注释改一个字，服务端在 IPClickServer 构造期间抛
    `ValueError: Level 'WARN' does not exist` —— 现象是"我就改了个日志级别，
    然后它起不来了"。

    注释已经修好，但别人的配置文件是从别处抄来的：几乎所有别的日志库都收 warn。
    收下这个别名比让服务崩掉强。
    """

    @pytest.mark.parametrize(
        "alias,canonical",
        [("warn", "WARNING"), ("WARN", "WARNING"), ("fatal", "CRITICAL"), ("err", "ERROR")],
    )
    def test_common_aliases_are_accepted(self, alias: str, canonical: str) -> None:
        from ipclick.utils.log_util import _LEVEL_ALIASES

        assert _LEVEL_ALIASES[alias.upper()] == canonical
        LogUtil.init(level=alias, logger_name=f"test-alias-{alias}")

    @pytest.mark.parametrize("level", ["trace", "debug", "info", "success", "warning", "error", "critical"])
    def test_loguru_native_levels_still_work(self, level: str) -> None:
        LogUtil.init(level=level, logger_name=f"test-native-{level}")

    def test_genuinely_unknown_level_still_raises(self) -> None:
        """收别名不等于什么都收——真的写错了还是要报错。"""
        with pytest.raises(ValueError, match="does not exist"):
            LogUtil.init(level="nonsense", logger_name="test-bogus")

    def test_the_shipped_template_lists_only_usable_levels(self) -> None:
        """模板注释里列的每一个级别都必须真的能用。

        这条守的是"文档撒谎"这件事本身：注释是给人照抄的，抄了崩溃就是缺陷。
        """
        from pathlib import Path
        import re

        template = Path(__file__).resolve().parent.parent / "src" / "ipclick" / "configs" / "default_config.toml"
        text = template.read_text(encoding="utf-8")
        match = re.search(r"# 日志级别 \(([^)]+)\)", text)
        assert match, "模板里找不到日志级别那行注释"
        for level in match.group(1).split("/"):
            LogUtil.init(level=level.strip(), logger_name=f"test-template-{level.strip()}")
