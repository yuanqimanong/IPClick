"""Web 端新增页面：请求流、试一试、配置写回、节点编辑。

配置写回那部分是真的往临时文件里写，然后再读回来解析——"注释有没有被保住"、
"改的是不是那一行"这类事，只有真写一遍再 parse 才能确认。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
import pathlib
from pathlib import Path
import re
import tomllib
from typing import Any, cast

import pytest

from ipclick.config_loader.writer import format_value, save, set_nodes, set_values
from ipclick.dto.response import Response
from ipclick.exceptions import ConfigError, ValidationError
from ipclick.services.task_service import TaskService
from ipclick.trace import TraceRecord, TraceSettings, init_recorder, reset_recorder
from ipclick.utils.config_util import Settings
from ipclick.web.editable import FIELDS, GROUPS, parse_form, parse_nodes, validate_nodes
from ipclick.web.pages import WebPages
from ipclick.web.templates import DEFAULT_LIVE_MS, LIVE_INTERVALS


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


class TestInlineTables:
    """模板里有些子表是内联写法（``viewport = {{ width = 1920, height = 1080 }}``）
    而不是独立的 ``[BROWSER.viewport]`` 节。

    0.5.0 之前往这种节写值会在文件末尾追加一个同名节头 → 同一个键声明两次 →
    文件下次启动直接解析不了。事故形状特别糟：写入成功、界面说"已保存"、服务
    照旧在跑，直到下次重启才炸，那时人早忘了自己在网页上改过什么。
    """

    SAMPLE = '[BROWSER]\nenabled = true\nviewport = { width = 1920, height = 1080 }\n'

    def test_updates_in_place(self):
        new, changes = set_values(self.SAMPLE, {"BROWSER.viewport": {"width": 1280}})
        assert tomllib.loads(new)["BROWSER"]["viewport"] == {"width": 1280, "height": 1080}
        assert changes == ["[BROWSER].viewport.width = 1280"]

    def test_never_appends_a_duplicate_section(self):
        new, _ = set_values(self.SAMPLE, {"BROWSER.viewport": {"width": 1280}})
        assert "[BROWSER.viewport]" not in new
        assert sum("viewport" in line for line in new.splitlines()) == 1

    def test_adds_a_missing_key_inside_the_table(self):
        new, _ = set_values(self.SAMPLE, {"BROWSER.viewport": {"scale": 2}})
        assert tomllib.loads(new)["BROWSER"]["viewport"]["scale"] == 2
        assert tomllib.loads(new)["BROWSER"]["viewport"]["width"] == 1920

    def test_commas_inside_quotes_are_not_split_points(self):
        """``body.split(",")`` 会把这一行切错，切错就等于把配置改坏。"""
        text = '[T]\nx = { ua = "a,b", n = 1 }\n'
        new, _ = set_values(text, {"T.x": {"n": 5}})
        assert tomllib.loads(new)["T"]["x"] == {"ua": "a,b", "n": 5}

    def test_nested_brackets_are_not_split_points(self):
        text = "[T]\nx = { args = [1, 2], n = 1 }\n"
        new, _ = set_values(text, {"T.x": {"n": 5}})
        assert tomllib.loads(new)["T"]["x"] == {"args": [1, 2], "n": 5}

    def test_trailing_comment_survives(self):
        text = "[T]\nx = { n = 1 }  # 别删我\n"
        new, _ = set_values(text, {"T.x": {"n": 5}})
        assert "# 别删我" in new

    def test_refuses_when_the_target_is_not_an_editable_table(self):
        """改不动就明说。猜着写下去的结果是产出一个开不了机的配置。"""
        with pytest.raises(ConfigError, match="不是可以就地编辑的形式"):
            _ = set_values("[T]\nx = 1\n", {"T.x": {"n": 5}})

    def test_a_genuinely_absent_section_is_still_appended(self):
        """真的整节都不存在时，追加行为不变——别把这个修法扩大到不该管的地方。"""
        new, changes = set_values("[T]\nx = 1\n", {"OTHER.sub": {"n": 5}})
        assert tomllib.loads(new)["OTHER"]["sub"]["n"] == 5
        assert "（新增节）" in changes[0]


class TestSaveValidatesToml:
    """写之前先确认还是合法 TOML。这个模块按行做文本编辑（为了保住注释和排版），
    代价就是存在产出非法 TOML 的可能——那种文件写进去之后要到下次重启才炸。
    """

    def test_refuses_to_write_invalid_toml(self, tmp_path: Path):
        target = tmp_path / "ipclick.toml"
        _ = target.write_text("[A]\nx = 1\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="不是合法 TOML"):
            _ = save(target, "[A]\n[A]\nx = 1\n")

    def test_the_original_file_is_left_alone(self, tmp_path: Path):
        target = tmp_path / "ipclick.toml"
        original = "[A]\nx = 1\n"
        _ = target.write_text(original, encoding="utf-8")
        with pytest.raises(ConfigError):
            _ = save(target, "this is not = = toml")
        assert target.read_text(encoding="utf-8") == original

    def test_valid_toml_still_writes(self, tmp_path: Path):
        target = tmp_path / "ipclick.toml"
        _ = target.write_text("[A]\nx = 1\n", encoding="utf-8")
        _ = save(target, "[A]\nx = 2\n")
        assert tomllib.loads(target.read_text(encoding="utf-8"))["A"]["x"] == 2


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


class TestEveryFieldRoundTrips:
    """白名单里的每一项，都必须能写回模板并原样读出来。

    这条测试挡的是加字段时最容易犯的三种错，它们的共同点是**页面上看着完全正常**：

    * 键名写错（配置键是 ``browser``，属性名是 ``kind`` —— 按属性名写会生成一个
      ``[BROWSER].kind``，谁都不读）
    * 节名写错（``[BROWSER].page_timeout`` 而不是 ``[BROWSER.timeout].page_load``）
    * 节在模板里只是个内联表，写回时被追加成重复节头，文件下次启动解析不了
    """

    def test_all_fields_write_back_and_read_back(self):
        template = pathlib.Path(
            str(Path(__import__("ipclick").__file__).parent / "configs" / "default_config.toml")
        ).read_text(encoding="utf-8")

        probe: dict[str, dict[str, Any]] = {}
        for field in FIELDS.values():
            if field.kind == "int":
                value: Any = int((field.minimum or 0) + 7)
            elif field.kind == "float":
                value = float((field.minimum or 0) + 1.5)
            elif field.kind == "bool":
                value = True
            elif field.kind == "choice":
                value = field.choices[-1]
            else:
                value = "probe-value"
            probe.setdefault(field.section, {})[field.key] = value

        new_text, changes = set_values(template, probe)
        assert len(changes) == len(FIELDS)

        data = tomllib.loads(new_text)  # 写坏了就炸在这一行
        for name, field in FIELDS.items():
            node: Any = data
            for part in field.section.split("."):
                assert isinstance(node, dict) and part in node, f"{name}：节 {field.section} 不见了"
                node = node[part]
            assert node[field.key] == probe[field.section][field.key], f"{name} 写回后读出来不一样"

    def test_every_field_key_exists_in_the_template(self):
        """模板里没有的键 = 用户在页面上看到的是 Field.default，而不是这台机器的实际配置。"""
        template = tomllib.loads(
            pathlib.Path(
                str(Path(__import__("ipclick").__file__).parent / "configs" / "default_config.toml")
            ).read_text(encoding="utf-8")
        )
        missing: list[str] = []
        for name, field in FIELDS.items():
            node: Any = template
            for part in field.section.split("."):
                node = node.get(part, {}) if isinstance(node, dict) else {}
            if not isinstance(node, dict) or field.key not in node:
                missing.append(name)
        assert not missing, f"这些项在 default_config.toml 里没有对应的键：{missing}"

    def test_declared_defaults_match_the_template(self):
        """``Field.default`` 必须是**代码里真正的默认值**。写错了的话，配置文件里
        没写这一项时页面会显示一个假值，用户一点保存就把假值固化进文件。
        """
        template = tomllib.loads(
            pathlib.Path(
                str(Path(__import__("ipclick").__file__).parent / "configs" / "default_config.toml")
            ).read_text(encoding="utf-8")
        )
        wrong: list[str] = []
        for name, field in FIELDS.items():
            if field.default is None:
                continue
            node: Any = template
            for part in field.section.split("."):
                node = node.get(part, {})
            actual = node.get(field.key)
            if isinstance(actual, bool) or isinstance(field.default, bool):
                same = bool(actual) == bool(field.default)
            else:
                same = str(actual) == str(field.default)
            if not same:
                wrong.append(f"{name}: 声明 {field.default!r}，模板里是 {actual!r}")
        assert not wrong, wrong


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


class TestClusterTab:
    """节点管理并进了配置页的「集群设置」——它本来就是集群配置的一部分。"""

    def test_add_node(self, pages: WebPages):
        html = pages.add_node(
            {"new_node_host": "10.0.0.7", "new_node_port": "9528", "new_node_id": "n7"},
            "admin",
            "csrf",
        )
        parsed = tomllib.loads(pages.config_path.read_text(encoding="utf-8"))
        assert parsed["CLUSTER"]["nodes"][0]["id"] == "n7"
        assert parsed["CLUSTER"]["nodes"][0]["address"] == "10.0.0.7:9528"
        assert "已添加节点" in html

    def test_add_node_defaults_the_port(self, pages: WebPages):
        """加机器是日常操作，每次让人把端口想一遍纯属拖慢。"""
        from ipclick.web.pages import NODE_PORT_BASE

        _ = pages.add_node({"new_node_host": "10.0.0.8"}, "admin", "csrf")
        parsed = tomllib.loads(pages.config_path.read_text(encoding="utf-8"))
        assert parsed["CLUSTER"]["nodes"][0]["address"] == f"10.0.0.8:{NODE_PORT_BASE}"
        # id 留空则用 host:port
        assert parsed["CLUSTER"]["nodes"][0]["id"] == f"10.0.0.8:{NODE_PORT_BASE}"

    def test_port_increments_past_used_ones(self, pages: WebPages):
        """连着加三台，端口应该是 19001 / 19002 / 19003，而不是全撞在一起。"""
        from ipclick.web.pages import NODE_PORT_BASE

        for index in range(3):
            _ = pages.add_node({"new_node_host": f"10.0.0.{index}"}, "u", "c")
        parsed = tomllib.loads(pages.config_path.read_text(encoding="utf-8"))
        ports = [int(n["address"].rpartition(":")[2]) for n in parsed["CLUSTER"]["nodes"]]
        assert ports == [NODE_PORT_BASE, NODE_PORT_BASE + 1, NODE_PORT_BASE + 2]

    def test_remove_node(self, pages: WebPages):
        """删除是独立按钮：和「保存」共用表单的话，点一次删除会把页面上其余
        未提交的改动一起写进去。"""
        _ = pages.add_node({"new_node_host": "10.0.0.1", "new_node_id": "keep"}, "u", "c")
        _ = pages.add_node({"new_node_host": "10.0.0.2", "new_node_id": "drop"}, "u", "c")
        html = pages.remove_node({"remove_node": "drop"}, "u", "c")
        parsed = tomllib.loads(pages.config_path.read_text(encoding="utf-8"))
        assert [n["id"] for n in parsed["CLUSTER"]["nodes"]] == ["keep"]
        assert "已移除节点 drop" in html

    def test_remove_unknown_node_is_reported(self, pages: WebPages):
        assert "没有 id 为" in pages.remove_node({"remove_node": "nope"}, "u", "c")

    def test_remove_keeps_other_pending_edits_out(self, pages: WebPages):
        """删除表单里只有节点 id，不带配置字段——所以不可能连带写进别的改动。"""
        _ = pages.add_node({"new_node_host": "10.0.0.1", "new_node_id": "a"}, "u", "c")
        before = tomllib.loads(pages.config_path.read_text(encoding="utf-8"))["SERVER"]["max_workers"]
        _ = pages.remove_node({"remove_node": "a", "SERVER.max_workers": "999"}, "u", "c")
        after = tomllib.loads(pages.config_path.read_text(encoding="utf-8"))["SERVER"]["max_workers"]
        assert after == before

    def test_add_node_needs_a_host(self, pages: WebPages):
        assert "请填 IP" in pages.add_node({"new_node_host": "  "}, "admin", "csrf")

    def test_add_node_rejects_duplicate_id(self, pages: WebPages):
        _ = pages.add_node({"new_node_host": "10.0.0.7", "new_node_id": "dup"}, "u", "c")
        assert "已经有一个" in pages.add_node({"new_node_host": "10.0.0.9", "new_node_id": "dup"}, "u", "c")

    def test_add_node_rejects_bad_port(self, pages: WebPages):
        assert "端口必须是数字" in pages.add_node({"new_node_host": "h", "new_node_port": "abc"}, "u", "c")

    def test_saving_the_cluster_tab_writes_nodes(self, pages: WebPages):
        html = pages.save_config(
            {"tab": "cluster", "node_id_0": "a", "node_address_0": "1.1.1.1:1", "node_weight_0": "100"},
            "admin",
            "csrf",
        )
        parsed = tomllib.loads(pages.config_path.read_text(encoding="utf-8"))
        assert parsed["CLUSTER"]["nodes"][0]["address"] == "1.1.1.1:1"
        assert "已写回" in html

    def test_clearing_an_address_deletes_the_node(self, pages: WebPages):
        _ = pages.add_node({"new_node_host": "1.1.1.1", "new_node_id": "gone"}, "u", "c")
        _ = pages.save_config({"tab": "cluster", "node_id_0": "gone", "node_address_0": ""}, "u", "c")
        parsed = tomllib.loads(pages.config_path.read_text(encoding="utf-8"))
        assert parsed["CLUSTER"]["nodes"] == []

    def test_bad_node_rejected(self, pages: WebPages):
        html = pages.save_config(
            {"tab": "cluster", "node_id_0": "a", "node_address_0": "not-an-address"}, "admin", "csrf"
        )
        assert "host:port" in html

    def test_forward_toggle_writes_on_off_not_true_false(self, pages: WebPages):
        """配置里是 "on"/"off" 字符串，写成 true/false 的话 ClusterConfig 不认。"""
        _ = pages.save_config(
            {"tab": "cluster", "__present__CLUSTER.forward_on": "1", "CLUSTER.forward_on": "on"}, "u", "c"
        )
        parsed = tomllib.loads(pages.config_path.read_text(encoding="utf-8"))
        assert parsed["CLUSTER"]["forward"] == "on"

        _ = pages.save_config({"tab": "cluster", "__present__CLUSTER.forward_on": "1"}, "u", "c")
        parsed = tomllib.loads(pages.config_path.read_text(encoding="utf-8"))
        assert parsed["CLUSTER"]["forward"] == "off"

    def test_existing_token_is_preserved(self, pages: WebPages):
        """一次网页保存不能把配置文件里手写的令牌抹掉。"""
        pages.config_path.write_text(
            '[CLUSTER]\nnodes = [\n  { id = "a", address = "1.1.1.1:1", token = "keepme" },\n]\n',
            encoding="utf-8",
        )
        pages.config = Settings({"CLUSTER": {"nodes": [{"id": "a", "address": "1.1.1.1:1", "token": "keepme"}]}})
        _ = pages.save_config(
            {"tab": "cluster", "node_id_0": "a", "node_address_0": "1.1.1.1:1", "node_weight_0": "100"}, "u", "c"
        )
        parsed = tomllib.loads(pages.config_path.read_text(encoding="utf-8"))
        assert parsed["CLUSTER"]["nodes"][0]["token"] == "keepme"


class TestOnlyChangedValuesAreReported:
    """表单一次会把整页字段都提交上来。不比对旧值的话，改一个日志级别会被告知
    "这 7 项需要重启"——人于是开始无视这句提示，而它在真需要时是唯一的信号。"""

    def test_unchanged_fields_are_not_written(self, pages: WebPages):
        from ipclick.web.editable import FIELDS, current_value

        field = FIELDS["SERVER.max_workers"]
        same = str(current_value(pages.config, field))
        html = pages.save_config({"tab": "basic", "SERVER.max_workers": same}, "u", "c")
        assert "没有可保存的改动" in html

    def test_changed_field_is_written_and_flagged(self, pages: WebPages):
        html = pages.save_config({"tab": "basic", "SERVER.max_workers": "77"}, "u", "c")
        assert "1 项" in html
        assert "worker 线程数" in html

    def test_int_float_equality_does_not_count_as_a_change(self, pages: WebPages):
        """toml 里写 60 读出来是 int，而超时是 float 字段，解析出来是 60.0。"""
        from ipclick.web.pages import _same_value  # pyright: ignore[reportPrivateUsage]

        assert _same_value(60, 60.0) is True
        # bool 是 int 的子类：True == 1 不能被当成"没变"
        assert _same_value(1, True) is False
        assert _same_value(True, True) is True


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
        assert f'data-live-interval="{DEFAULT_LIVE_MS}"' in html
        assert 'http-equiv="refresh"' not in html, "不该再整页重载"

    def test_live_can_be_turned_off(self, pages: WebPages):
        """关掉的表达方式是间隔 0，**不是**把 data-live-src 去掉。

        0.4 是去掉属性，于是前端再也找不到那个元素，"关闭"这一档一按就没法
        在前端重新打开——必须重新提交表单整页重载，而重载正好丢掉这一页最
        在意的滚动位置。
        """
        html = pages.trace_page({"_": "", "live": "0"}, "admin", "csrf")
        assert 'data-live-interval="0"' in html
        assert "data-live-src=" in html, "元素要留着，否则前端开不回来"
        assert "实时刷新已关闭" in html

    def test_live_empty_value_still_means_off(self, pages: WebPages):
        """0.4 的复选框不勾选时提交的是空串。老链接不该变成"默认开"。"""
        assert 'data-live-interval="0"' in pages.trace_page({"_": "", "live": ""}, "admin", "csrf")

    def test_live_legacy_checkbox_value_maps_to_default(self, pages: WebPages):
        """``live=1`` 是 0.4 复选框的取值。老书签点开该落到默认档，
        而不是掉进"取值不认识"的分支变成关闭。
        """
        html = pages.trace_page({"_": "", "live": "1"}, "admin", "csrf")
        assert f'data-live-interval="{DEFAULT_LIVE_MS}"' in html

    @pytest.mark.parametrize("ms", [ms for ms, _, _ in LIVE_INTERVALS])
    def test_live_every_tier_round_trips(self, pages: WebPages, ms: int):
        html = pages.trace_page({"_": "", "live": str(ms)}, "admin", "csrf")
        assert f'data-live-interval="{ms}"' in html
        assert f'id="live-{ms}" name="live" value="{ms}" checked' in html, "该档的按钮要是选中态"

    def test_live_rejects_arbitrary_intervals(self, pages: WebPages):
        """只认档位表里的值。放开任意值的话，有人填 100ms，服务端的 worker
        线程就全耗在渲染这一页上了。
        """
        for bogus in ("50", "-1000", "999999", "abc,def"):
            html = pages.trace_page({"_": "", "live": bogus}, "admin", "csrf")
            assert f'data-live-interval="{DEFAULT_LIVE_MS}"' in html, bogus

    def test_live_control_has_no_inline_handler(self, pages: WebPages):
        """CSP 只放行两段脚本的 sha256 哈希，哈希覆盖不到事件处理器属性。
        写了 onchange= 会被静默拦掉——表现为"点了没反应"，最难查的那一类。
        """
        html = pages.trace_page({}, "admin", "csrf")
        assert "onchange=" not in html
        assert "onclick=" not in html

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


class TestSkillPage:
    """AI 接入页。技能包本身在 tests/test_skill.py 测，这里只测它在 Web 端的出口。"""

    def test_page_renders_the_packaged_skill(self, pages: WebPages):
        from ipclick import skill

        html = pages.skill_page("admin", "csrf")
        assert "ipclick skill install" in html
        assert skill.description()[:20] in html

    def test_markdown_is_the_same_document_as_the_cli(self, pages: WebPages):
        """两个出口给的必须是同一份——差一个字都会让"照页面做"和"照 CLI 做"
        得到不同结论。"""
        from ipclick import skill

        assert pages.skill_markdown() == skill.markdown()

    def test_page_offers_the_raw_download(self, pages: WebPages):
        html = pages.skill_page("admin", "csrf")
        assert 'href="/skill.md"' in html

    def test_skill_body_is_escaped(self, pages: WebPages):
        """技能正文里有 <b> 之类的字面量吗？没有也要转义——这一页嵌的是别的
        文件的内容，不转义就是给未来埋雷。"""
        html = pages.skill_page("admin", "csrf")
        assert "<script>" not in pages.skill_markdown() or "&lt;script&gt;" in html


class TestTestPageParameters:
    """「试一试」的表单字段必须和 SDK 的 request() 对齐。

    对不齐的后果很隐蔽：页面上试通了，代码里多传一个 proxy 就不通——而这一页
    存在的全部理由就是"这里看到的行为等于线上行为"。
    """

    def _request(self, pages: WebPages, form: dict[str, str]):
        return pages._build_request({"url": "http://example.com/x", **form}, "http://example.com/x")  # pyright: ignore[reportPrivateUsage]

    def test_query_params_become_json(self, pages: WebPages):
        """协议里 params 是一个字符串字段（服务端按 JSON 解析），不是 map。"""
        import json

        request = self._request(pages, {"params": "foo=bar\nq=中文"})
        assert json.loads(request.params) == {"foo": "bar", "q": "中文"}

    def test_cookies_are_parsed(self, pages: WebPages):
        request = self._request(pages, {"cookies": "sid=abc\ntheme=dark"})
        assert dict(request.cookies) == {"sid": "abc", "theme": "dark"}

    def test_json_body_uses_the_json_field(self, pages: WebPages):
        """data 与 json 是两个互斥字段：发 json 时服务端才会带 Content-Type。"""
        request = self._request(pages, {"body": '{"a":1}', "body_kind": "json"})
        assert request.json == '{"a":1}'
        assert not request.data

    def test_raw_body_uses_the_data_field(self, pages: WebPages):
        request = self._request(pages, {"body": "a=1&b=2", "body_kind": "raw"})
        assert request.data == b"a=1&b=2"
        assert not request.json

    def test_bad_json_body_is_rejected(self, pages: WebPages):
        with pytest.raises(ValidationError, match="JSON"):
            self._request(pages, {"body": "{不是 json", "body_kind": "json"})

    def test_custom_proxy_passes_through(self, pages: WebPages):
        request = self._request(pages, {"proxy_mode": "custom", "proxy_url": "http://p:8080"})
        assert request.proxy == "http://p:8080"

    def test_config_proxy_without_config_is_rejected(self, pages: WebPages):
        """选了「用配置里的代理」但 [PROXY] 是空的——静默直连会让人以为验过了代理。"""
        with pytest.raises(ValidationError, match=r"\[PROXY\]"):
            self._request(pages, {"proxy_mode": "config"})

    def test_no_proxy_by_default(self, pages: WebPages):
        assert self._request(pages, {}).proxy == ""

    def test_impersonate_only_for_curl_cffi(self, pages: WebPages):
        assert self._request(pages, {"impersonate": "safari180"}).impersonate == "safari180"
        # 换成浏览器适配器时这一项没有意义，不该发过去
        assert self._request(pages, {"adapter": "browser", "impersonate": "safari180"}).impersonate == ""

    def test_retries_default_to_zero(self, pages: WebPages):
        """诊断路径要看的是**第一次**失败的真实原因。"""
        assert self._request(pages, {}).max_retries == 0

    def test_retries_are_capped(self, pages: WebPages):
        from ipclick.web.pages import TEST_RETRIES_MAX

        assert self._request(pages, {"max_retries": "999"}).max_retries == TEST_RETRIES_MAX

    def test_backoff_only_sent_with_retries(self, pages: WebPages):
        assert not self._request(pages, {"retry_backoff": "3"}).HasField("retry_backoff_seconds")
        assert self._request(pages, {"max_retries": "2", "retry_backoff": "3"}).retry_backoff_seconds == 3.0

    def test_switches_default_on(self, pages: WebPages):
        """未提交过表单时（比如从 curl 导入）两个开关按 SDK 的默认值来。"""
        request = self._request(pages, {"verify": "on", "allow_redirects": "on"})
        assert request.verify_ssl is True
        assert request.allow_redirects is True

    def test_switches_can_be_turned_off(self, pages: WebPages):
        request = self._request(pages, {})
        assert request.verify_ssl is False
        assert request.allow_redirects is False

    def test_allowed_status_codes(self, pages: WebPages):
        assert list(self._request(pages, {"allowed_status_codes": "200, 404"}).allowed_status_codes) == [200, 404]

    def test_bad_status_code_is_rejected(self, pages: WebPages):
        """写错一个字符就整项丢掉的话，用户以为设了而实际没设。"""
        with pytest.raises(ValidationError, match="非数字"):
            self._request(pages, {"allowed_status_codes": "200, abc"})

    def test_bad_automation_config_is_rejected(self, pages: WebPages):
        with pytest.raises(ValidationError, match="JSON"):
            self._request(pages, {"automation_config": "{oops"})

    def test_timeout_is_capped(self, pages: WebPages):
        from ipclick.web.pages import TEST_TIMEOUT_MAX

        assert self._request(pages, {"timeout": "9999"}).timeout_seconds == TEST_TIMEOUT_MAX

    def test_bad_number_names_the_field(self, pages: WebPages):
        with pytest.raises(ValidationError, match="超时"):
            self._request(pages, {"timeout": "abc"})

    def test_page_exposes_every_field(self, pages: WebPages):
        html = pages.test_page({}, None, "admin", "csrf")
        for name in (
            "params",
            "cookies",
            "proxy_mode",
            "proxy_url",
            "impersonate",
            "max_retries",
            "retry_backoff",
            "allowed_status_codes",
            "verify",
            "allow_redirects",
            "automation_config",
            "body_kind",
        ):
            assert f'name="{name}"' in html, name

    def test_script_box_hidden_when_server_disallows(self, pages: WebPages):
        """allow_scripts 关着时给出理由，而不是放一个填了也不生效的框。"""
        html = pages.test_page({}, None, "admin", "csrf")
        assert 'name="automation_script"' not in html
        assert "allow_scripts" in html

    def test_retry_hint_matches_the_real_cap(self):
        """提示文案和真正的上界分居两个模块，必须盯着它们不失步。"""
        from ipclick.web.pages import TEST_RETRIES_MAX
        from ipclick.web.templates import TEST_RETRIES_MAX_HINT

        assert TEST_RETRIES_MAX == TEST_RETRIES_MAX_HINT


class TestTargetNodes:
    def test_nodes_listed_even_without_forwarding(self, pages: WebPages):
        """0.4 只在开了转发时才显示这个下拉框。配了节点却看不到它的人，
        并不知道那层区别，只会觉得功能没做。"""
        pages.config = Settings(
            {"CLUSTER": {"nodes": [{"id": "a", "address": "10.0.0.1:9528"}]}}  # 没有 forward = "on"
        )
        nodes = pages._target_nodes()  # pyright: ignore[reportPrivateUsage]
        assert [n["id"] for n in nodes] == ["a"]
        assert nodes[0]["forwarding"] is False

    def test_no_nodes_means_no_selector(self, pages: WebPages):
        pages.config = Settings({"CLUSTER": {}})
        assert pages._target_nodes() == []  # pyright: ignore[reportPrivateUsage]
        assert 'name="target_node"' not in pages.test_page({}, None, "admin", "csrf")

    def test_unknown_node_is_rejected(self, pages: WebPages):
        pages.config = Settings({"CLUSTER": {"nodes": [{"id": "a", "address": "10.0.0.1:9528"}]}})
        result = pages.run_test({"url": "http://example.com/x", "target_node": "nope"})
        assert result.get("error_only") is True
        assert "不在集群节点列表里" in result["error"]


class TestRemoteComponents:
    """在**某台子节点**上装组件。

    集群里每台机器都要各自装一遍适配器，逐台 SSH 上去敲命令是部署时最烦的一步。
    这一组守的是那条路的两端：默认拒绝，以及拒绝时说得清怎么打开。
    """

    def test_unknown_node_is_reported(self, pages: WebPages):
        pages.config = Settings({"CLUSTER": {"nodes": [{"id": "a", "address": "10.0.0.1:19001"}]}})
        result = pages.remote_component("nope", "list")
        assert result["ok"] is False
        assert "不在集群节点列表里" in result["message"]

    def test_unreachable_node_is_readable(self, pages: WebPages):
        """连不上时给一句人话，而不是一坨 gRPC 状态码。"""
        pages.config = Settings({"CLUSTER": {"nodes": [{"id": "a", "address": "127.0.0.1:1"}]}})
        result = pages.remote_component("a", "list")
        assert result["ok"] is False
        assert "连不上节点 a" in result["message"]

    def test_local_path_unchanged_when_no_node_given(self, pages: WebPages):
        """不选机器时还是走本机那条路，一个字都不该变。"""
        ok, message = pages.component_action("install", "不存在的组件")
        assert ok is False
        assert "未知的组件" in message

    def test_default_is_off(self):
        """打开它等于"能调本节点 gRPC 的人可以在本机跑 pip"，不能随升级默认获得。"""
        from ipclick.services.task_service import TaskService

        service = TaskService(Settings({}))
        try:
            assert service.remote_install_allowed is False
        finally:
            service.cleanup()

    def test_opt_in_flag(self):
        from ipclick.services.task_service import TaskService

        service = TaskService(Settings({"CLUSTER": {"allow_remote_install": True}}))
        try:
            assert service.remote_install_allowed is True
        finally:
            service.cleanup()

    def test_denied_when_off(self):
        """拒绝时必须说清要改哪一项——笼统的"失败"等于把答案藏起来。"""
        import grpc

        from ipclick.dto.proto import task_pb2
        from ipclick.services.task_service import TaskService

        class _Ctx:
            code = None
            details = ""

            def set_code(self, code: object) -> None:
                self.code = code

            def set_details(self, details: str) -> None:
                self.details = details

        service = TaskService(Settings({}))
        context = _Ctx()
        try:
            response = service.Component(task_pb2.ComponentReq(op="list"), cast(Any, context))
        finally:
            service.cleanup()
        assert response.ok is False
        assert context.code is grpc.StatusCode.PERMISSION_DENIED
        assert "allow_remote_install" in context.details

    def test_list_when_allowed(self):
        import json

        from ipclick.dto.proto import task_pb2
        from ipclick.services.task_service import TaskService

        class _Ctx:
            def set_code(self, code: object) -> None: ...
            def set_details(self, details: str) -> None: ...

        service = TaskService(Settings({"CLUSTER": {"allow_remote_install": True}}))
        try:
            response = service.Component(task_pb2.ComponentReq(op="list"), cast(Any, _Ctx()))
        finally:
            service.cleanup()
        assert response.ok is True
        names = {c["name"] for c in json.loads(response.components_json)}
        assert "camoufox" in names

    def test_unknown_op_is_rejected(self):
        import grpc

        from ipclick.dto.proto import task_pb2
        from ipclick.services.task_service import TaskService

        class _Ctx:
            code = None

            def set_code(self, code: object) -> None:
                self.code = code

            def set_details(self, details: str) -> None: ...

        service = TaskService(Settings({"CLUSTER": {"allow_remote_install": True}}))
        context = _Ctx()
        try:
            response = service.Component(task_pb2.ComponentReq(op="rm -rf /"), cast(Any, context))
        finally:
            service.cleanup()
        assert response.ok is False
        assert context.code is grpc.StatusCode.INVALID_ARGUMENT

    def test_whitelist_still_applies_remotely(self):
        """包名走白名单这条在远程一侧同样成立——对端跑的是同一个 InstallManager。"""
        from ipclick.dto.proto import task_pb2
        from ipclick.services.task_service import TaskService

        class _Ctx:
            def set_code(self, code: object) -> None: ...
            def set_details(self, details: str) -> None: ...

        service = TaskService(Settings({"CLUSTER": {"allow_remote_install": True}}))
        try:
            response = service.Component(
                task_pb2.ComponentReq(op="install", extra="requests; rm -rf /"), cast(Any, _Ctx())
            )
        finally:
            service.cleanup()
        assert response.ok is False
        assert "未知的组件" in response.message

    def test_job_shape_matches_the_local_one(self):
        """前端那段渲染进度条的 JS 是同一份，两边差一个字段名就会在远程那条路上
        静默失效。"""
        from ipclick.dto.proto import task_pb2
        from ipclick.web.installer import Job
        from ipclick.web.pages import _job_from_pb  # pyright: ignore[reportPrivateUsage]

        local = Job(id="j", title="t", command=("x",)).snapshot()
        remote = _job_from_pb(task_pb2.ComponentJob(id="j", title="t", command="x", percent=-1.0))
        assert set(remote) == set(local)
        assert set(remote["progress"]) == set(local["progress"])
        # -1 是"量不出来"的哨兵（proto3 的 double 没有 null）
        assert remote["progress"]["percent"] is None


class TestRuntimePortDisclosure:
    """`ipclick run --port X` 不改配置文件，于是配置页那一格显示的是文件里的
    9528，而进程实际在 X 上。0.5.0 之前页面对此一个字都不说——这是"端口有歧义"
    那条反馈里最难查的一半：改它、保存、重启，一切"正常"，只是端口不是那个。
    """

    def test_mismatch_is_called_out(self, pages: WebPages):
        pages.runtime_ports = {"SERVER.port": 10086}
        html = pages.config_page("admin", "csrf")
        assert "当前实际在 10086" in html

    def test_silent_when_they_agree(self, pages: WebPages):
        from ipclick.web.editable import FIELDS, current_value

        pages.runtime_ports = {"SERVER.port": int(current_value(pages.config, FIELDS["SERVER.port"]))}
        assert "当前实际在" not in pages.config_page("admin", "csrf")

    def test_silent_when_nothing_was_overridden(self, pages: WebPages):
        pages.runtime_ports = {}
        assert "当前实际在" not in pages.config_page("admin", "csrf")

    def test_display_only_never_written_back(self, pages: WebPages):
        """只影响显示。要是它能被写回文件，`--port` 就会在下次保存时被固化进
        配置——那是把一个临时覆盖变成永久的，没人会预期。
        """
        from ipclick.web.editable import FIELDS, current_value

        pages.runtime_ports = {"SERVER.port": 10086}
        _ = pages.config_page("admin", "csrf")
        assert int(current_value(pages.config, FIELDS["SERVER.port"])) != 10086


class TestTimezone:
    """时间列必须按**看的人**的时区显示，不是服务端的。

    服务端渲染 ``datetime.fromtimestamp()`` 得到的是服务端本地时间。只有一台机器、
    人就坐在它旁边时没问题；一旦服务端跑在 UTC 的容器里（Docker 默认就是），
    东八区的人看到的每一条都慢八小时。而且症状很温和——"时间看着像那么回事，
    就是和自己的表对不上"——很少有人会把它当成 bug 报出来。
    """

    def test_record_iso_carries_an_offset(self):
        """不带偏移量的话浏览器只能猜，猜错就是差几个钟头。"""
        record = TraceRecord(
            ts=1_755_000_000.0, uuid="u", node_id="n", adapter="curl_cffi",
            method="GET", url="http://x", status_code=200, duration_ms=1, size=1,
        )
        assert re.search(r"[+-]\d{2}:\d{2}$", record.iso), f"没有时区偏移：{record.iso}"
        assert datetime.fromisoformat(record.iso).timestamp() == pytest.approx(1_755_000_000.0, abs=1)

    def test_trace_table_emits_a_time_element(self, pages: WebPages):
        _ = pages.run_test({"url": "http://example.com/tz"})
        html = pages.trace_page({}, "admin", "csrf")
        assert "<time datetime=" in html

    def test_the_fallback_text_is_still_a_readable_time(self, pages: WebPages):
        """没有 JS 时标签里的文字要仍然可读（维持旧行为），不能变成空白。"""
        _ = pages.run_test({"url": "http://example.com/tz"})
        html = pages.trace_page({}, "admin", "csrf")
        match = re.search(r"<time datetime=\"[^\"]+\">([^<]+)</time>", html)
        assert match is not None
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", match.group(1))

    def test_dashboard_recent_requests_too(self):
        """总览的「最近请求」和请求流走的是同一个表格渲染函数，别只修一处。"""
        from ipclick.web.templates import render_dashboard

        record = TraceRecord(
            ts=1_755_000_000.0, uuid="u", node_id="n", adapter="curl_cffi",
            method="GET", url="http://example.com/tz", status_code=200, duration_ms=1, size=1,
        )
        html = render_dashboard({"recent": [record], "trace": {}}, "admin", "csrf", False)
        assert "<time datetime=" in html

    def test_daily_buckets_say_they_are_server_side(self):
        """按天趋势是 SQLite 按服务端时区分好的桶——浏览器换算只能挪显示，
        挪不动已经分好的桶。所以要如实标注，别让人以为是自己那边的"今天"。
        """
        from ipclick.web.templates import _daily

        html = _daily([{"day": "2026-08-18", "total": 3, "ok": 2, "failed": 1, "avg_ms": 12}])
        assert "服务端本地时区" in html
