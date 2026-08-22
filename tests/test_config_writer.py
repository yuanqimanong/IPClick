from __future__ import annotations

from pathlib import Path
import tomllib

import pytest

from ipclick.config_loader.writer import save, set_values
from ipclick.exceptions import ConfigError


def test_inline_table_update_preserves_escaped_quotes_and_commas() -> None:
    source = '[CLUSTER]\ndiscovery = { mode = "dns", name = "a\\"b,c", refresh = 30 }\n'

    updated, changes = set_values(source, {"CLUSTER.discovery": {"refresh": 60}})

    parsed = tomllib.loads(updated)
    assert parsed["CLUSTER"]["discovery"] == {"mode": "dns", "name": 'a"b,c', "refresh": 60}
    assert changes == ["[CLUSTER].discovery.refresh = 60"]


def test_save_does_not_reuse_a_predictable_temp_file(tmp_path: Path) -> None:
    target = tmp_path / "ipclick.toml"
    stale_temp = tmp_path / "ipclick.toml.tmp"
    stale_temp.write_text("do not touch", encoding="utf-8")

    save(target, "[SERVER]\nport = 9528\n", backup=False)

    assert tomllib.loads(target.read_text(encoding="utf-8"))["SERVER"]["port"] == 9528
    assert stale_temp.read_text(encoding="utf-8") == "do not touch"
    assert not list(tmp_path.glob(".ipclick.toml.*.tmp"))


def test_a_subtable_key_can_be_added_when_the_parent_section_already_exists() -> None:
    """父节存在、子表不存在时应当补一个子表节，而不是让整批修改一起失败。

    原来直接抛 ConfigError「请手工编辑这个文件」，而且因为是在循环中途抛的，
    同一次提交里其它已算好的修改全部丢掉。Web 配置页有 21 个字段落在点分节里
    （BROWSER.viewport / DOWNLOADER.retry / LOG.rotation …），手写的精简 ipclick.toml
    上这条必然踩到。
    """
    text = '[SERVER]\nport = 50051\n\n[BROWSER]\nengine = "playwright"\n'

    out, changes = set_values(text, {"SERVER": {"port": 50052}, "BROWSER.viewport": {"width": 1920}})

    parsed = tomllib.loads(out)
    assert parsed["SERVER"]["port"] == 50052, "同批次的其它修改被丢掉了"
    assert parsed["BROWSER"]["viewport"]["width"] == 1920
    assert parsed["BROWSER"]["engine"] == "playwright"
    assert len(changes) == 2


def test_an_uneditable_subtable_still_refuses_but_only_that_one() -> None:
    """子表确实存在却不是可就地编辑的形式时，仍然明确拒绝。"""
    text = "[BROWSER]\nviewport = {\n  width = 1280,\n}\n"

    with pytest.raises(ConfigError, match="viewport"):
        _ = set_values(text, {"BROWSER.viewport": {"width": 1920}})


@pytest.mark.parametrize(
    "hostile",
    [
        "x = 1\n[SECURITY]\nauth_token",
        "a\nb",
        'q"uote',
        "with space",
        "bracket]",
        "",
    ],
)
def test_hostile_key_names_are_refused_instead_of_injecting_toml(hostile: str) -> None:
    """键名是**原样**拼进输出的，必须在拼之前挡住。

    值有转义（关不掉自己的字符串、也换不了行），键名和节名一个都没有。而 save()
    只用 tomllib.loads 兜底——注入出来的东西恰好是**合法** TOML 时它拦不住：
    实测 {'SERVER': {'x = 1\\n[SECURITY]\\nauth_token': 'pwned'}} 能把调用方原有的
    port/host 整体搬进新造的 [SECURITY] 表，并写进文件。
    """
    with pytest.raises(ConfigError):
        _ = set_values('[SERVER]\nport = 50051\nhost = "127.0.0.1"\n', {"SERVER": {hostile: "pwned"}})


@pytest.mark.parametrize("hostile", ['JUNK]\n[SECURITY]\nauth_token = "pwned"\n[X', "a b", 'x"y', "", "."])
def test_hostile_section_names_are_refused(hostile: str) -> None:
    with pytest.raises(ConfigError):
        _ = set_values("[SERVER]\nport = 50051\n", {hostile: {"y": 1}})


@pytest.mark.parametrize("char", ["\x00", "\x1b", "\x07", "\x0b", "\x7f"])
def test_control_characters_in_a_value_round_trip_instead_of_being_blamed_on_ipclick(char: str) -> None:
    """值里带控制字符时应当正常转义写出，而不是产出非法 TOML。

    原来 format_value 只转义 \\ " \\n \\r \\t，其余控制字符原样落进基本字符串，
    save() 的 tomllib 校验拒绝写入并告诉用户"这是 IPClick 自己的 bug，请提 issue"
    ——而这其实只是用户输入里有个控制字符。
    """
    value = f"/var/log/a{char}b.log"

    out, _ = set_values('[LOG]\noutput = "x"\n', {"LOG": {"output": value}})

    assert tomllib.loads(out)["LOG"]["output"] == value


def test_a_multiline_value_is_refused_with_an_honest_message() -> None:
    """跨行的值（数组换行写）不能被当成单行改掉，那会把后面几行变成孤儿。

    原来按"一个键一行"直接重写那一行，剩下的 "old-b", ] 留在原地，产出非法 TOML，
    再被 save() 报成"IPClick 自己的 bug"。[SECURITY].auth_token 的轮换写法正是数组。
    """
    text = '[SECURITY]\nauth_token = [\n  "old-a",\n  "old-b",\n]\nrequire = true\n'

    with pytest.raises(ConfigError, match="跨行"):
        _ = set_values(text, {"SECURITY": {"auth_token": "new"}})
