"""Web 配置页对齐 0.6 / 0.7 新增的并发配置项。

「能在页面上显示」和「能保存进配置文件并生效」是两回事——这组测试守的是后者。
少了任何一项，用户就只能去编辑 ipclick.toml，而配置页给人的承诺是"改完就生效"。
"""

import pytest

from ipclick.web.editable import FIELDS, GROUPS


NEW_IN_060_070 = [
    "SERVER.processes",
    "SERVER.max_concurrent_rpcs",
    "SERVER.max_concurrent_streams",
    "SERVER.compression",
    "SERVER.async_mode",
]


class TestNewFieldsAreEditable:
    @pytest.mark.parametrize("name", NEW_IN_060_070)
    def test_field_exists(self, name: str) -> None:
        assert name in FIELDS, f"{name} 没进可编辑列表，用户只能去手改 toml"

    @pytest.mark.parametrize("name", NEW_IN_060_070)
    def test_field_has_a_default_matching_the_code(self, name: str) -> None:
        """default 必须是**代码里真正的默认值**。

        展示成空白的话，用户一保存就把空值写进配置——等于点了一下「保存」
        就悄悄改了行为，而他什么都没动。
        """
        assert FIELDS[name].default is not None, f"{name} 缺 default"

    @pytest.mark.parametrize("name", NEW_IN_060_070)
    def test_field_explains_itself(self, name: str) -> None:
        """这几项都不是望文生义的，hint 必须说清楚。"""
        assert len(FIELDS[name].hint) > 20, f"{name} 的 hint 太短，说不清它是干什么的"

    def test_appears_in_the_basic_tab(self) -> None:
        """并发相关的项该在「基础设置」里，不该落进集群分页。"""
        from ipclick.web.editable import groups_for

        names = {f.name for _, fields in groups_for("basic") for f in fields}
        for name in NEW_IN_060_070:
            assert name in names, f"{name} 不在基础设置分页里"


class TestParsingRoundTrip:
    def test_processes_accepts_zero_for_auto(self) -> None:
        """0 = 按 CPU 核数自动，必须能填进去。"""
        assert FIELDS["SERVER.processes"].parse("0") == 0

    def test_processes_rejects_negative(self) -> None:
        from ipclick.exceptions import ValidationError

        with pytest.raises(ValidationError):
            FIELDS["SERVER.processes"].parse("-1")

    def test_compression_is_a_closed_choice(self) -> None:
        """压缩方式只有三个合法值，填错了要当场拒绝而不是写进配置让服务起不来。"""
        from ipclick.exceptions import ValidationError

        assert FIELDS["SERVER.compression"].parse("none") == "none"
        with pytest.raises(ValidationError):
            FIELDS["SERVER.compression"].parse("brotli")

    def test_async_mode_is_a_bool(self) -> None:
        assert FIELDS["SERVER.async_mode"].parse("on") is True
        assert FIELDS["SERVER.async_mode"].parse("") is False

    def test_limits_accept_zero_meaning_auto(self) -> None:
        assert FIELDS["SERVER.max_concurrent_rpcs"].parse("0") == 0
        assert FIELDS["SERVER.max_concurrent_streams"].parse("0") == 0


class TestSavedValuesLandInTheConfigFile:
    def test_round_trip_through_the_writer(self) -> None:
        """保存之后配置文本里真的有这些键——这是「改完就生效」的前提。

        写回是**定点文本替换**（只换等号右边、保留注释与排版），所以还要确认
        原有的注释没被这次写入吃掉。
        """
        import tomllib

        from ipclick.config_loader.writer import set_values

        original = (
            "[SERVER]\n"
            "# 这行注释必须活下来\n"
            "port = 9528\n"
            "max_workers = 100\n"
            "processes = 1\n"
            "max_concurrent_rpcs = 0\n"
            'compression = "gzip"\n'
            "async_mode = false\n"
        )
        updated, changed = set_values(
            original,
            {
                "SERVER": {
                    "processes": 4,
                    "max_concurrent_rpcs": 2048,
                    "compression": "none",
                    "async_mode": True,
                }
            },
        )
        parsed = tomllib.loads(updated)["SERVER"]
        assert parsed["processes"] == 4
        assert parsed["max_concurrent_rpcs"] == 2048
        assert parsed["compression"] == "none"
        assert parsed["async_mode"] is True
        assert "这行注释必须活下来" in updated, "写回把注释吃掉了"
        assert changed, "没有报告任何改动项"


class TestDashboardExplainsMultiprocessTrace:
    def test_shape_row_warns_about_partial_trace(self) -> None:
        """多进程下链路记录是每进程一份，页面只看得到 0 号进程。

        不说明的话，人看到"记录只有四分之一"会去查链路配置、查磁盘、查 SQLite，
        而真相只是它本来就只统计了一个进程。
        """
        from ipclick.web.templates import _concurrency_shape

        single = _concurrency_shape({"processes": 1, "async_mode": False})
        multi = _concurrency_shape({"processes": 4, "async_mode": False})
        assert "单进程" in single
        assert "1/4" in multi and "0 号进程" in multi, f"多进程下没有提示链路记录只覆盖一个进程：{multi}"

    def test_shape_row_shows_async(self) -> None:
        from ipclick.web.templates import _concurrency_shape

        assert "异步" in _concurrency_shape({"processes": 1, "async_mode": True})
