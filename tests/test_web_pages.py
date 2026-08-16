"""Web 端新增页面：请求流、试一试、配置写回、节点编辑。

配置写回那部分是真的往临时文件里写，然后再读回来解析——"注释有没有被保住"、
"改的是不是那一行"这类事，只有真写一遍再 parse 才能确认。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import tomllib
from typing import Any

import pytest

from ipclick.config_loader.writer import format_value, set_nodes, set_values
from ipclick.dto.response import Response
from ipclick.services.task_service import TaskService
from ipclick.trace import TraceSettings, init_recorder, reset_recorder
from ipclick.utils.config_util import Settings
from ipclick.web.editable import FIELDS, GROUPS, parse_form, parse_nodes, validate_nodes
from ipclick.web.pages import WebPages


SAMPLE = """\
# 顶部说明
[SERVER]
# worker 线程数：每个请求占一个
max_workers = 10
port = 9527  # 端口

[DOWNLOADER]
download_timeout = 60

[DOWNLOADER.retry]
# 重试次数
max_attempts = 3
"""


class TestFormatValue:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (True, "true"),
            (False, "false"),
            (10, "10"),
            (1.5, "1.5"),
            ("abc", '"abc"'),
            ("", '""'),
            (["a", "b"], '["a", "b"]'),
        ],
    )
    def test_literals(self, value: object, expected: str):
        assert format_value(value) == expected

    def test_bool_before_int(self):
        """bool 是 int 的子类，判断顺序错了会写出 1 / 0。"""
        assert format_value(True) == "true"

    def test_quotes_escaped(self):
        assert format_value('a"b\\c') == '"a\\"b\\\\c"'

    def test_newline_escaped(self):
        """控制字符在 TOML 基本字符串里非法，必须转义否则文件解析不了。"""
        literal = format_value("a\nb")
        assert tomllib.loads("k = " + literal)["k"] == "a\nb"


class TestSetValues:
    def test_replaces_value_in_place(self):
        text, changes = set_values(SAMPLE, {"SERVER": {"max_workers": 32}})
        assert "max_workers = 32" in text
        assert changes == ["[SERVER].max_workers = 32"]
        assert tomllib.loads(text)["SERVER"]["max_workers"] == 32

    def test_comments_are_preserved(self):
        """整体 dump 会把注释全抹掉，而那些注释是这份配置最有价值的部分。"""
        text, _ = set_values(SAMPLE, {"SERVER": {"max_workers": 32}})
        assert "# worker 线程数：每个请求占一个" in text
        assert "# 顶部说明" in text

    def test_inline_comment_kept(self):
        text, _ = set_values(SAMPLE, {"SERVER": {"port": 10086}})
        assert "port = 10086  # 端口" in text

    def test_subtable(self):
        text, _ = set_values(SAMPLE, {"DOWNLOADER.retry": {"max_attempts": 7}})
        parsed = tomllib.loads(text)
        assert parsed["DOWNLOADER"]["retry"]["max_attempts"] == 7
        assert parsed["DOWNLOADER"]["download_timeout"] == 60, "不该动到别的节"

    def test_new_key_in_existing_section(self):
        text, changes = set_values(SAMPLE, {"DOWNLOADER": {"connect_timeout": 5.5}})
        assert tomllib.loads(text)["DOWNLOADER"]["connect_timeout"] == 5.5
        assert "新增" in changes[0]

    def test_new_section(self):
        text, changes = set_values(SAMPLE, {"TRACE": {"sqlite_enabled": True}})
        assert tomllib.loads(text)["TRACE"]["sqlite_enabled"] is True
        assert "新增节" in changes[0]

    def test_commented_example_is_not_treated_as_the_real_key(self):
        """模板里有大量 `# key = ...` 的示例，改到那一行等于改了个注释。"""
        text = '[CLUSTER]\n# secret = "example"\nself_id = ""\n'
        new_text, _ = set_values(text, {"CLUSTER": {"self_id": "node-a"}})
        assert '# secret = "example"' in new_text
        assert tomllib.loads(new_text)["CLUSTER"]["self_id"] == "node-a"

    def test_hash_inside_string_is_not_a_comment(self):
        text = '[WEB]\ncolor = "#fff"\n'
        new_text, _ = set_values(text, {"WEB": {"color": "#000"}})
        assert tomllib.loads(new_text)["WEB"]["color"] == "#000"

    def test_multiple_sections_at_once(self):
        text, changes = set_values(SAMPLE, {"SERVER": {"max_workers": 4}, "DOWNLOADER": {"download_timeout": 15}})
        parsed = tomllib.loads(text)
        assert parsed["SERVER"]["max_workers"] == 4
        assert parsed["DOWNLOADER"]["download_timeout"] == 15
        assert len(changes) == 2


class TestSetNodes:
    def test_writes_node_array(self):
        text = set_nodes(
            '[CLUSTER]\nforward = "on"\nnodes = []\n',
            [
                {"id": "a", "address": "10.0.0.1:9527", "weight": 100},
                {"id": "b", "address": "10.0.0.2:9527", "weight": 50},
            ],
        )
        parsed = tomllib.loads(text)
        assert [n["address"] for n in parsed["CLUSTER"]["nodes"]] == ["10.0.0.1:9527", "10.0.0.2:9527"]
        assert parsed["CLUSTER"]["nodes"][1]["weight"] == 50
        assert parsed["CLUSTER"]["forward"] == "on"

    def test_replaces_multiline_array_with_comments(self):
        text = (
            "[CLUSTER]\n"
            "nodes = [\n"
            '    # { id = "old", address = "1.1.1.1:1" },\n'
            '    { id = "keep", address = "2.2.2.2:2" },\n'
            "]\n"
            'load_balancer = "random"\n'
        )
        new_text = set_nodes(text, [{"id": "new", "address": "3.3.3.3:3", "weight": 100}])
        parsed = tomllib.loads(new_text)
        assert [n["id"] for n in parsed["CLUSTER"]["nodes"]] == ["new"]
        assert parsed["CLUSTER"]["load_balancer"] == "random", "数组后面的键不能被吃掉"

    def test_empty_list(self):
        text = set_nodes('[CLUSTER]\nnodes = [\n  { id = "a", address = "1.1.1.1:1" },\n]\n', [])
        assert tomllib.loads(text)["CLUSTER"]["nodes"] == []

    def test_token_written_when_present(self):
        text = set_nodes("[CLUSTER]\n", [{"id": "a", "address": "1.1.1.1:1", "weight": 100, "token": "t"}])
        assert tomllib.loads(text)["CLUSTER"]["nodes"][0]["token"] == "t"


class TestEditableWhitelist:
    def test_security_is_not_editable(self):
        """这是这份白名单最重要的性质：网页不能关掉 SSRF 防护、不能改令牌。"""
        forbidden = {
            "SECURITY.auth_token",
            "SECURITY.block_private_networks",
            "SECURITY.block_metadata_endpoints",
            "SECURITY.allowed_schemes",
            "SECURITY.allow_secrets_in_config",
            "WEB.username",
            "WEB.password",
            "CLUSTER.secret",
            "BROWSER.allow_scripts",
        }
        assert forbidden & set(FIELDS) == set()

    def test_no_section_named_security(self):
        assert not any(f.section.startswith("SECURITY") for f in FIELDS.values())

    def test_unknown_form_key_is_ignored(self):
        """手工构造的 POST 不能写进白名单外的任何配置项。"""
        updates, _, errors = parse_form({"SECURITY.block_private_networks": "false", "WEB.password": "x"})
        assert updates == {}
        assert errors == []

    def test_int_validation(self):
        _, _, errors = parse_form({"SERVER.max_workers": "abc"})
        assert errors and "整数" in errors[0]

    def test_range_validation(self):
        _, _, errors = parse_form({"SERVER.max_workers": "0"})
        assert errors and "不能小于" in errors[0]

    def test_choice_validation(self):
        _, _, errors = parse_form({"LOG.level": "verbose"})
        assert errors and "可选值" in errors[0]

    def test_bool_needs_present_marker(self):
        """复选框没勾时浏览器不提交这个键，靠隐藏标记区分"没勾"和"没在表单里"。"""
        updates, _, _ = parse_form({"TRACE.sqlite_enabled": "1"})
        assert updates == {}, "没有 present 标记就不该当成一次提交"

        updates, _, _ = parse_form({"__present__TRACE.sqlite_enabled": "1", "TRACE.sqlite_enabled": "1"})
        assert updates["TRACE"]["sqlite_enabled"] is True

        updates, _, _ = parse_form({"__present__TRACE.sqlite_enabled": "1"})
        assert updates["TRACE"]["sqlite_enabled"] is False

    def test_restart_flags_reported(self):
        _, restart, _ = parse_form({"SERVER.max_workers": "8"})
        assert restart == ["worker 线程数"]

    def test_live_applicable_fields_not_flagged(self):
        _, restart, _ = parse_form({"LOG.level": "debug"})
        assert restart == []

    def test_every_group_has_fields(self):
        assert all(fields for _, fields in GROUPS)

    def test_every_field_resolves_against_the_shipped_config(self):
        """护栏：白名单里的每一项都要能在随包默认配置里取到值。

        取不到（section/key 写错，或配置里根本没这一项）的症状是页面上一个空输入框，
        而用户一点保存就把空值写进配置文件——悄悄改了行为。所以要么配置里有，
        要么 Field 上填了 default。
        """
        import tomllib

        from ipclick.config_loader.loader import DEFAULT_CONFIG_PATH
        from ipclick.web.editable import current_value

        config = tomllib.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
        missing = [name for name, field in FIELDS.items() if current_value(config, field) is None]
        assert missing == [], f"这些项在默认配置里取不到值，也没有 default: {missing}"


class TestParseNodes:
    def test_existing_and_new(self):
        nodes = parse_nodes(
            {
                "node_id_0": "a",
                "node_address_0": "10.0.0.1:9527",
                "node_weight_0": "100",
                "new_node_address": "10.0.0.9:9527",
            }
        )
        assert [n["address"] for n in nodes] == ["10.0.0.1:9527", "10.0.0.9:9527"]
        assert nodes[1]["id"] == "10.0.0.9:9527", "id 留空时用地址"

    def test_blank_address_deletes_the_row(self):
        nodes = parse_nodes({"node_address_0": "", "node_id_0": "a", "node_address_1": "10.0.0.2:1"})
        assert [n["address"] for n in nodes] == ["10.0.0.2:1"]

    def test_non_contiguous_indexes(self):
        nodes = parse_nodes({"node_address_0": "1.1.1.1:1", "node_address_5": "2.2.2.2:2"})
        assert len(nodes) == 2

    def test_bad_address_reported(self):
        assert validate_nodes([{"id": "a", "address": "no-port"}])

    def test_duplicate_id_reported(self):
        """重复 id 会让两台机器共用一份健康状态，轮询也会错。"""
        errors = validate_nodes([{"id": "same", "address": "1.1.1.1:1"}, {"id": "same", "address": "2.2.2.2:2"}])
        assert any("重复" in e for e in errors)

    def test_valid_nodes_pass(self):
        assert validate_nodes([{"id": "a", "address": "10.0.0.1:9527", "weight": 100}]) == []


class FakeAdapter:
    adapter_name: str = "curl_cffi"

    def download(self, url: str, **kwargs: Any) -> Response:
        return Response(
            url=url,
            status_code=200,
            content=b"<html><body>hello <script>alert(1)</script></body></html>",
            headers={"Content-Type": "text/html"},
        )

    def close(self) -> None: ...


@pytest.fixture
def pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[WebPages]:
    monkeypatch.chdir(tmp_path)
    config_file = tmp_path / "ipclick.toml"
    config_file.write_text(SAMPLE, encoding="utf-8")

    # 先初始化进程级记录器再建 TaskService——后者在构造时就取了单例，
    # 顺序反了的话埋点会落到另一个实例上（这正是生产代码里的顺序）。
    recorder = init_recorder(TraceSettings(memory_size=50))
    service = TaskService(Settings({"SECURITY": {"block_private_networks": False}}))
    adapter = FakeAdapter()
    service._adapter_cache["curl_cffi"] = adapter  # pyright: ignore[reportPrivateUsage, reportArgumentType]
    service.default_adapter = adapter  # pyright: ignore[reportAttributeAccessIssue]

    try:
        yield WebPages(
            Settings({"SERVER": {"max_workers": 10}, "CLUSTER": {}}),
            recorder,
            task_service=service,
            config_path=config_file,
        )
    finally:
        service.cleanup()
        reset_recorder()


class TestTestPage:
    def test_runs_a_real_request_through_the_service(self, pages: WebPages):
        result = pages.run_test({"url": "http://example.com/x", "adapter": "curl_cffi", "method": "GET"})
        assert result["status_code"] == 200
        assert "hello" in result["body"]
        assert result["trace"]["adapter"] == "curl_cffi"

    def test_request_shows_up_in_the_trace_feed(self, pages: WebPages):
        _ = pages.run_test({"url": "http://example.com/x"})
        records, _ = pages.recorder.query()
        assert records and records[0].url == "http://example.com/x"

    def test_body_is_escaped_in_the_page(self, pages: WebPages):
        """返回的源码里可能有 <script>——直接插进页面就是 XSS。"""
        result = pages.run_test({"url": "http://example.com/x"})
        html = pages.test_page({}, result, "admin", "csrf")
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html

    def test_missing_url_is_rejected(self, pages: WebPages):
        assert pages.run_test({"url": ""})["error_only"] is True

    def test_non_http_scheme_rejected(self, pages: WebPages):
        result = pages.run_test({"url": "file:///etc/passwd"})
        assert result["error_only"] is True

    def test_unknown_adapter_rejected(self, pages: WebPages):
        result = pages.run_test({"url": "http://example.com", "adapter": "nope"})
        assert result["error_only"] is True

    def test_timeout_is_capped(self, pages: WebPages):
        """页面是同步等结果的，不能让它等十分钟占着 worker。"""
        request = pages._build_request({"timeout": "99999"}, "http://example.com")  # pyright: ignore[reportPrivateUsage]
        assert request.timeout_seconds <= 120

    def test_headers_parsed_line_by_line(self, pages: WebPages):
        request = pages._build_request(  # pyright: ignore[reportPrivateUsage]
            {"headers": "X-A: 1\nX-B: 2\ngarbage"}, "http://example.com"
        )
        assert dict(request.headers) == {"X-A": "1", "X-B": "2"}


class TestAdapterChoices:
    """P2-1：下拉框要**分类展示全部**，而不是只列本机装了的。

    0.3 只返回注册表里已装的那几个，于是没装的适配器直接从列表里消失——
    对着 wiki 看的人会觉得"文档和实现对不上"，也不知道到底支持哪些。
    """

    @staticmethod
    def _flat(pages: WebPages) -> dict[str, dict[str, object]]:
        choices = pages._adapter_choices()  # pyright: ignore[reportPrivateUsage]
        return {item["value"]: item for group in choices for item in group["items"]}

    def test_groups_are_http_and_browser(self, pages: WebPages):
        titles = [g["title"] for g in pages._adapter_choices()]  # pyright: ignore[reportPrivateUsage]
        assert titles == ["HTTP 适配器", "浏览器渲染"]

    def test_core_adapter_always_available(self, pages: WebPages):
        items = self._flat(pages)
        assert items["curl_cffi"]["available"] is True

    def test_every_extra_is_listed_even_if_missing(self, pages: WebPages):
        """关键回归：没装的也要在列表里（置灰 + 安装命令），不能消失。"""
        from ipclick.components import COMPONENTS

        items = self._flat(pages)
        for component in COMPONENTS:
            assert component.name in items, f"{component.name} 从下拉框里消失了"

    def test_missing_ones_are_disabled_with_install_hint(self, pages: WebPages, monkeypatch: pytest.MonkeyPatch):
        from ipclick.utils import module_probe

        monkeypatch.setattr(module_probe, "installed", lambda _name: False)
        items = self._flat(pages)
        assert items["niquests"]["available"] is False
        assert 'pip install "ipclick[niquests]"' in str(items["niquests"]["hint"])

    def test_browser_is_a_placeholder_not_a_sixth_component(self, pages: WebPages):
        """browser 是"引擎由服务端自动选"的占位值，必须和真实组件名区分开——
        混排会让人以为它是文档漏掉的第六个 extra。
        """
        pages.config = Settings({"BROWSER": {"enabled": True}})
        items = self._flat(pages)
        assert "browser" in items
        assert "自动选择" in str(items["browser"]["label"])

    def test_browser_disabled_when_rendering_off(self, pages: WebPages):
        pages.config = Settings({"BROWSER": {"enabled": False}})
        items = self._flat(pages)
        # 仍然列出来，但选不了——直接消失的话没人知道是"关掉了"还是"不支持"
        assert items["browser"]["available"] is False
        assert "enabled = false" in str(items["browser"]["hint"])


class TestConfigPage:
    def test_renders_current_values(self, pages: WebPages):
        html = pages.config_page("admin", "csrf")
        assert 'name="SERVER.max_workers"' in html
        assert 'value="10"' in html

    def test_secrets_are_never_rendered(self, pages: WebPages):
        pages.config = Settings({"SECURITY": {"auth_token": "super-secret-token"}})
        html = pages.config_page("admin", "csrf")
        assert "super-secret-token" not in html
        assert "已配置" in html

    def test_save_writes_back_and_keeps_comments(self, pages: WebPages):
        html = pages.save_config({"SERVER.max_workers": "48"}, "admin", "csrf")
        text = pages.config_path.read_text(encoding="utf-8")
        assert tomllib.loads(text)["SERVER"]["max_workers"] == 48
        assert "# worker 线程数：每个请求占一个" in text
        assert "已写回" in html

    def test_save_makes_a_backup(self, pages: WebPages):
        _ = pages.save_config({"SERVER.max_workers": "48"}, "admin", "csrf")
        assert pages.config_path.with_suffix(".toml.bak").exists()

    def test_restart_hint_is_shown(self, pages: WebPages):
        html = pages.save_config({"SERVER.max_workers": "48"}, "admin", "csrf")
        assert "要重启" in html

    def test_invalid_value_does_not_touch_the_file(self, pages: WebPages):
        before = pages.config_path.read_text(encoding="utf-8")
        html = pages.save_config({"SERVER.max_workers": "-5"}, "admin", "csrf")
        assert pages.config_path.read_text(encoding="utf-8") == before
        assert "不能小于" in html

    def test_log_level_applies_live(self, pages: WebPages):
        _ = pages.save_config({"LOG.level": "debug"}, "admin", "csrf")
        # 保存提示里不该出现"这些项要重启"——日志级别是当场生效的
        messages, _ = pages._take_flash()  # pyright: ignore[reportPrivateUsage]
        assert not any("要重启" in m for m in messages)


class TestNodesPage:
    def test_save_nodes(self, pages: WebPages):
        html = pages.save_nodes(
            {"new_node_address": "10.0.0.7:9527", "new_node_id": "n7", "new_node_weight": "100"},
            "admin",
            "csrf",
        )
        parsed = tomllib.loads(pages.config_path.read_text(encoding="utf-8"))
        assert parsed["CLUSTER"]["nodes"][0]["id"] == "n7"
        assert "已写回" in html

    def test_bad_node_rejected(self, pages: WebPages):
        html = pages.save_nodes({"new_node_address": "not-an-address"}, "admin", "csrf")
        assert "host:port" in html

    def test_existing_token_is_preserved(self, pages: WebPages):
        """一次网页保存不能把配置文件里手写的令牌抹掉。"""
        pages.config_path.write_text(
            '[CLUSTER]\nnodes = [\n  { id = "a", address = "1.1.1.1:1", token = "keepme" },\n]\n',
            encoding="utf-8",
        )
        pages.config = Settings({"CLUSTER": {"nodes": [{"id": "a", "address": "1.1.1.1:1", "token": "keepme"}]}})
        _ = pages.save_nodes({"node_id_0": "a", "node_address_0": "1.1.1.1:1", "node_weight_0": "100"}, "u", "c")
        parsed = tomllib.loads(pages.config_path.read_text(encoding="utf-8"))
        assert parsed["CLUSTER"]["nodes"][0]["token"] == "keepme"


class TestTracePage:
    def test_lists_records(self, pages: WebPages):
        _ = pages.run_test({"url": "http://example.com/one"})
        html = pages.trace_page({}, "admin", "csrf")
        assert "http://example.com/one" in html

    def test_live_refresh_on_by_default(self, pages: WebPages):
        """0.4 改成局部刷新：``<meta refresh>`` 每 3 秒重载整页，滚动位置丢失、
        正在填的过滤条件被冲掉、页面白闪。现在只换那一块的 innerHTML。
        """
        html = pages.trace_page({}, "admin", "csrf")
        assert "data-live-src=" in html
        assert 'http-equiv="refresh"' not in html, "不该再整页重载"

    def test_live_can_be_turned_off(self, pages: WebPages):
        html = pages.trace_page({"_": "", "live": ""}, "admin", "csrf")
        assert "data-live-src=" not in html

    def test_live_fragment_keeps_the_filters(self, pages: WebPages):
        """刷新片段必须带上过滤条件，否则刷一次就把筛选冲掉了。"""
        html = pages.trace_page({"status": "5xx", "adapter": "curl_cffi"}, "admin", "csrf")
        assert "status=5xx" in html
        assert "adapter=curl_cffi" in html

    def test_fragment_and_full_page_render_the_same_table(self, pages: WebPages):
        """片段和整页走同一个渲染函数——两套逻辑迟早对不上，而那种失步
        只有在数据变化时才暴露，最难查。
        """
        _ = pages.run_test({"url": "http://example.com/frag"})
        fragment = pages.trace_fragment({})
        assert "http://example.com/frag" in fragment
        assert fragment in pages.trace_page({}, "admin", "csrf")

    def test_status_filter(self, pages: WebPages):
        _ = pages.run_test({"url": "http://example.com/ok"})
        assert "http://example.com/ok" not in pages.trace_page({"status": "5xx"}, "admin", "csrf")

    def test_json_api(self, pages: WebPages):
        _ = pages.run_test({"url": "http://example.com/j"})
        payload = pages.trace_json({})
        assert payload["records"][0]["url"] == "http://example.com/j"
        assert payload["source"] == "memory"

    def test_limit_is_capped(self, pages: WebPages):
        payload = pages.trace_json({"limit": "99999"})
        assert int(payload["filters"]["limit"]) <= 1000

    def test_garbage_limit_falls_back(self, pages: WebPages):
        assert pages.trace_json({"limit": "abc"})["filters"]["limit"] == "100"


class TestDashboardExtras:
    """P2-2：总览要覆盖**全部五个** extras，不只是"渲染引擎"。

    0.3 那张表只有四个浏览器引擎，niquests 是纯 HTTP 适配器、不属于渲染引擎，
    于是它完全没有展示位——装没装只能靠猜。
    """

    def test_all_five_extras_are_reported(self, pages: WebPages):
        names = {c["name"] for c in pages.dashboard_extras()["components"]}
        assert names == {"niquests", "camoufox", "patchright", "playwright", "DrissionPage"}

    def test_niquests_has_a_slot(self, pages: WebPages):
        """回归：0.3 里 niquests 完全没有安装状态展示位。"""
        components = {c["name"]: c for c in pages.dashboard_extras()["components"]}
        assert "package" in components["niquests"]

    def test_package_and_browser_body_stay_separate(self, pages: WebPages):
        """两级状态不能合并：只报一个的话，pip 装完但没 fetch 的机器会显示
        "已安装"，而第一次请求会卡几分钟去下 1 GB。
        """
        components = {c["name"]: c for c in pages.dashboard_extras()["components"]}
        camoufox = components["camoufox"]
        assert "package" in camoufox and "browser" in camoufox
        assert camoufox["browser_command"] == "python -m camoufox fetch"

    def test_components_carry_install_commands(self, pages: WebPages):
        assert all(c["install"].startswith("pip install") for c in pages.dashboard_extras()["components"])

    def test_reported_even_when_rendering_disabled(self, pages: WebPages):
        """关掉浏览器渲染不该让组件清单消失——"装没装"和"开不开"是两件事，
        而 niquests 根本不受 [BROWSER].enabled 影响。
        """
        pages.config = Settings({"BROWSER": {"enabled": False}})
        assert len(pages.dashboard_extras()["components"]) == 5


class TestBrowserHangFixes:
    """针对「Web 试一试用 browser 会卡死」那轮排查的回归。

    实测过一次点击 296 秒（node-c 日志：`completed in 296123ms`），
    根因是几个放大器叠在一起，每一个都单独修了。
    """

    def test_diagnostic_path_does_not_retry(self, pages: WebPages):
        """回归：不显式设 max_retries 会回落到服务端的 max_attempts=3，
        一次点击变成 4 次完整请求。诊断要看的是第一次失败的真实原因。
        """
        request = pages._build_request({}, "http://example.com")  # pyright: ignore[reportPrivateUsage]
        assert request.HasField("max_retries"), "必须显式设，否则会继承生产重试策略"
        assert request.max_retries == 0

    def test_post_redirects_instead_of_rendering(self, pages: WebPages):
        """回归：POST 直接渲染结果时，用户按 F5 会把整次请求重新提交一遍——
        而这一页的一次提交可能是几十秒的真实浏览器渲染。
        """
        result = pages.run_test({"url": "http://example.com/x"})
        token = pages.stash_test_result({"url": "http://example.com/x"}, result)
        assert token

        form, restored = pages.take_test_result(token)
        assert form["url"] == "http://example.com/x"
        assert restored is not None
        assert restored["status_code"] == result["status_code"]

    def test_unknown_result_token_is_a_blank_form(self, pages: WebPages):
        """token 过期或被伪造时当成一次普通打开，不该报错。"""
        assert pages.take_test_result("nope") == ({}, None)
        assert pages.take_test_result("") == ({}, None)

    def test_stash_is_bounded(self, pages: WebPages):
        """暂存区不是历史记录，不能无限长。"""
        from ipclick.web.pages import TEST_RESULT_KEEP

        tokens = [pages.stash_test_result({}, {"status_code": i}) for i in range(TEST_RESULT_KEEP + 8)]
        alive = [t for t in tokens if pages.take_test_result(t)[1] is not None]
        assert len(alive) == TEST_RESULT_KEEP
        assert alive == tokens[-TEST_RESULT_KEEP:], "该淘汰最旧的"

    def test_page_warns_about_browser_cold_start(self, pages: WebPages):
        """页面同步等结果又没有转圈动画，必须用文字说清楚，否则用户会重复点击，
        而每多点一次就多一份真实的浏览器渲染。
        """
        html = pages.test_page({}, None, "admin", "csrf")
        assert "冷启动" in html
        assert "重复点击" in html
        assert "不重试" in html
