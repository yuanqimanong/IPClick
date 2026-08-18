"""给程序 / AI 调用的那组命令（``ipclick.cli.agent``）。

这些测试守的是**输出契约**，不是措辞：`--json` 时 stdout 必须能被 json.loads
吃下去、每个文档都带 ok 与 exit_code、退出码分类正确、机密不外泄。
这几条一旦破了，调用方（多半是个模型）不会报错，只会静默地做错事。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from click.testing import CliRunner
import pytest

from ipclick.cli.main import main
from ipclick.cli.output import Exit


@pytest.fixture
def runner() -> CliRunner:
    # stderr 单独收：契约的核心就是"stdout 只有 JSON"，混在一起就测不出来了
    return CliRunner()


def payload(result: Any) -> dict[str, Any]:
    """把 stdout 解析成 JSON。解析不了直接失败并把原文摊出来。"""
    try:
        return json.loads(result.stdout)
    except ValueError as e:  # pragma: no cover - 只在契约被破坏时走到
        raise AssertionError(f"stdout 不是合法 JSON（{e}）：{result.stdout[:400]!r}") from e


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    path = tmp_path / "ipclick.toml"
    path.write_text(
        '[SERVER]\nhost = "127.0.0.1"\nport = 19999\n\n'
        '[SECURITY]\nauth_token = "sup3r-s3cret"\n\n'
        '[PROXY]\nhost = "p.example.com"\nport = 8080\nauth_password = "proxy-pw"\n\n'
        "[TRACE]\nsqlite_enabled = false\n",
        encoding="utf-8",
    )
    return path


class TestJSONContract:
    """--json 时 stdout 上有且只有一个 JSON 文档，成功失败都是。"""

    def test_success_is_a_single_document(self, runner: CliRunner, cfg: Path):
        result = runner.invoke(main, ["node", "list", "-c", str(cfg), "--json"])
        assert result.exit_code == 0
        assert payload(result)["ok"] is True

    def test_failure_is_also_json(self, runner: CliRunner, cfg: Path):
        """失败甩到 stderr 的话，调用方得写两套解析逻辑。"""
        result = runner.invoke(main, ["trace", "list", "-c", str(cfg), "--json"])
        data = payload(result)
        assert data["ok"] is False
        assert "sqlite_enabled" in data["error"]

    def test_every_document_carries_ok_and_exit_code(self, runner: CliRunner, cfg: Path):
        """很多代理框架只把 stdout 递回去，拿不到进程退出码。"""
        result = runner.invoke(main, ["trace", "stats", "-c", str(cfg), "--json"])
        data = payload(result)
        assert data["exit_code"] == result.exit_code

    def test_chinese_is_not_escaped(self, runner: CliRunner, cfg: Path):
        """ensure_ascii=True 出来的 \\uXXXX 谁也 grep 不到。"""
        result = runner.invoke(main, ["trace", "list", "-c", str(cfg), "--json"])
        assert "\\u" not in result.stdout


class TestExitCodes:
    def test_broken_config_is_rejected_not_silently_ignored(self, runner: CliRunner, tmp_path: Path):
        """load_config 对坏文件是"跳过并打日志"（服务端不该因此起不来）。
        但对这一组命令那等于用着默认值假装用户的配置生效了。"""
        bad = tmp_path / "broken.toml"
        bad.write_text("[SERVER\nport = ", encoding="utf-8")
        result = runner.invoke(main, ["config", "show", "-c", str(bad), "--json"])
        assert result.exit_code == Exit.REJECTED
        assert "TOML" in payload(result)["error"]

    def test_missing_config_file_is_rejected(self, runner: CliRunner, tmp_path: Path):
        result = runner.invoke(main, ["config", "show", "-c", str(tmp_path / "nope.toml"), "--json"])
        assert result.exit_code == Exit.REJECTED

    def test_unknown_config_path_is_rejected(self, runner: CliRunner, cfg: Path):
        result = runner.invoke(main, ["config", "get", "-c", str(cfg), "NOPE.nothing", "--json"])
        assert result.exit_code == Exit.REJECTED
        assert payload(result)["ok"] is False

    def test_unreachable_server_is_3_not_1(self, runner: CliRunner, cfg: Path):
        """「连不上 IPClick」和「目标网站 404」要去查的东西完全不同。"""
        result = runner.invoke(main, ["status", "-c", str(cfg), "-p", "19999", "--json"])
        assert result.exit_code == Exit.UNREACHABLE
        assert payload(result)["server"]["healthy"] is False

    def test_usage_error_is_2(self, runner: CliRunner, cfg: Path):
        result = runner.invoke(main, ["fetch", "http://x/", "-c", str(cfg), "-X", "TELEPORT"])
        assert result.exit_code == Exit.USAGE

    def test_unknown_adapter_is_usage_error(self, runner: CliRunner, cfg: Path):
        result = runner.invoke(main, ["fetch", "http://x/", "-c", str(cfg), "-a", "netscape"])
        assert result.exit_code == Exit.USAGE
        assert "netscape" in result.output


class TestSecretsNeverLeak:
    """这几条命令的输出很可能被原样贴进日志、issue 或一个模型的上下文里。"""

    def test_config_show_redacts(self, runner: CliRunner, cfg: Path):
        result = runner.invoke(main, ["config", "show", "-c", str(cfg), "--json"])
        assert "sup3r-s3cret" not in result.stdout
        assert "proxy-pw" not in result.stdout
        assert payload(result)["config"]["SECURITY"]["auth_token"] == "<已配置>"

    def test_config_get_redacts_a_secret_leaf(self, runner: CliRunner, cfg: Path):
        result = runner.invoke(main, ["config", "get", "-c", str(cfg), "SECURITY.auth_token", "--json"])
        assert "sup3r-s3cret" not in result.stdout

    def test_config_get_returns_plain_values(self, runner: CliRunner, cfg: Path):
        result = runner.invoke(main, ["config", "get", "-c", str(cfg), "SERVER.port", "--json"])
        assert payload(result)["value"] == 19999

    def test_status_reports_token_presence_not_value(self, runner: CliRunner, cfg: Path):
        result = runner.invoke(main, ["status", "-c", str(cfg), "--json"])
        assert "sup3r-s3cret" not in result.stdout
        assert payload(result)["security"]["auth_token_configured"] is True

    def test_node_list_never_echoes_token(self, runner: CliRunner, tmp_path: Path):
        path = tmp_path / "c.toml"
        path.write_text(
            '[CLUSTER]\nnodes = [{ id = "a", address = "h:1", token = "node-token" }]\n',
            encoding="utf-8",
        )
        result = runner.invoke(main, ["node", "list", "-c", str(path), "--json"])
        assert "node-token" not in result.stdout
        assert payload(result)["nodes"][0]["has_token"] is True


class TestTrace:
    def test_sqlite_off_says_why(self, runner: CliRunner, cfg: Path):
        """空列表会被读成"最近没有请求"，那是完全错误的结论。"""
        result = runner.invoke(main, ["trace", "list", "-c", str(cfg), "--json"])
        assert result.exit_code == Exit.REJECTED
        assert "请求流" in payload(result)["error"]

    def test_missing_db_mentions_the_resolved_path(self, runner: CliRunner, tmp_path: Path):
        path = tmp_path / "c.toml"
        path.write_text(
            '[SERVER]\nport = 12345\n\n[TRACE]\nsqlite_enabled = true\nsqlite_path = "t.{port}.db"\n',
            encoding="utf-8",
        )
        result = runner.invoke(main, ["trace", "list", "-c", str(path), "--json"])
        assert "t.12345.db" in payload(result)["error"]

    def test_reads_a_real_database(self, runner: CliRunner, tmp_path: Path):
        from ipclick.trace import SQLiteSink, TraceRecord

        db = tmp_path / "trace.db"
        sink = SQLiteSink(str(db), retention_days=0)
        sink.submit(
            TraceRecord(
                ts=1_700_000_000.0,
                uuid="u1",
                node_id="n1",
                adapter="curl_cffi",
                method="GET",
                url="https://example.com/a",
                status_code=200,
                duration_ms=12,
                size=34,
            )
        )
        sink.close()

        path = tmp_path / "c.toml"
        path.write_text(
            f'[TRACE]\nsqlite_enabled = true\nsqlite_path = "{db.as_posix()}"\n',
            encoding="utf-8",
        )
        result = runner.invoke(main, ["trace", "list", "-c", str(path), "--json"])
        assert result.exit_code == 0
        records = payload(result)["records"]
        assert len(records) == 1
        assert records[0]["url"] == "https://example.com/a"
        assert records[0]["status_class"] == "2xx"

    def test_reader_does_not_create_a_database(self, tmp_path: Path):
        """只读入口指着一个不存在的路径时不该当场造一个空库出来——
        那会让人以为"记录丢了"，而真正的问题是路径写错了。"""
        from ipclick.trace import TraceReader

        missing = tmp_path / "nope.db"
        reader = TraceReader(str(missing))
        assert reader.exists() is False
        assert reader.query() == []
        assert not missing.exists()


class TestComponents:
    def test_list_is_json(self, runner: CliRunner, cfg: Path):
        result = runner.invoke(main, ["component", "list", "-c", str(cfg), "--json"])
        assert result.exit_code == 0
        names = {c["name"] for c in payload(result)["components"]}
        assert {"niquests", "camoufox", "patchright", "playwright", "DrissionPage"} <= names

    def test_unknown_extra_is_rejected(self, runner: CliRunner):
        """包名走白名单常量。这条是安全边界，不是易用性。"""
        result = runner.invoke(main, ["component", "install", "requests; rm -rf /", "--json"])
        assert result.exit_code == Exit.REJECTED
        assert payload(result)["ok"] is False

    def test_dry_run_shows_the_command_without_running_it(self, runner: CliRunner):
        result = runner.invoke(main, ["component", "install", "niquests", "--dry-run", "--json"])
        assert result.exit_code == 0
        data = payload(result)
        assert data["dry_run"] is True
        # 命令是列表交给 subprocess 的（shell=False），不是一条拼出来的字符串
        assert isinstance(data["command"], list)
        assert "install" in data["command"]

    def test_plan_is_shared_with_the_web_side(self):
        """Web 端和 CLI 必须走同一份规划逻辑——那里才是白名单的位置。"""
        from ipclick.web.installer import plan

        prepared, reason = plan("install", "camoufox")
        assert reason == ""
        assert prepared is not None
        assert prepared.component.extra == "camoufox"

        rejected, why = plan("install", "camoufox; curl evil.sh")
        assert rejected is None
        assert "未知的组件" in why


class TestHelpSurface:
    """`--help` 是 AI 的第二信息源（第一是 SKILL.md），它必须自洽。"""

    @pytest.mark.parametrize(
        "argv",
        [
            ["fetch", "--help"],
            ["status", "--help"],
            ["trace", "--help"],
            ["node", "--help"],
            ["component", "--help"],
            ["config", "--help"],
            ["skill", "--help"],
        ],
    )
    def test_help_works(self, runner: CliRunner, argv: list[str]):
        result = runner.invoke(main, argv)
        assert result.exit_code == 0

    def test_every_machine_command_takes_json(self, runner: CliRunner):
        """漏一个 --json，调用方就得为那一条单独写解析。"""
        for argv in (
            ["fetch", "--help"],
            ["status", "--help"],
            ["trace", "list", "--help"],
            ["trace", "stats", "--help"],
            ["node", "list", "--help"],
            ["node", "probe", "--help"],
            ["component", "list", "--help"],
            ["config", "show", "--help"],
            ["config", "get", "--help"],
            ["skill", "show", "--help"],
        ):
            result = runner.invoke(main, argv)
            assert "--json" in result.output, argv

    def test_top_level_lists_the_new_groups(self, runner: CliRunner):
        output = runner.invoke(main, ["--help"]).output
        for name in ("fetch", "status", "trace", "node", "component", "skill"):
            assert name in output


class TestInstallRestartHint:
    """从 CLI 装组件是**另一个进程**：磁盘上有了，正在跑的服务端还按启动时那份
    适配器注册表工作。症状极具迷惑性（status 说就绪、fetch 说缺依赖），必须明说。
    """

    def test_successful_install_says_restart_is_needed(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch):
        import ipclick.cli.agent as agent_module

        monkeypatch.setattr(agent_module, "_quiet_logs", lambda: None)
        # 不真的装：这条测的是"说了没有"，不是 pip 能不能跑
        monkeypatch.setattr("ipclick.web.installer.execute", lambda command, on_line, **kw: 0)

        result = runner.invoke(main, ["component", "install", "niquests", "--json"])
        assert result.exit_code == 0
        data = payload(result)
        assert data["restart_required"] is True
        assert "重启" in data["hint"]

    def test_dry_run_does_not_claim_a_restart(self, runner: CliRunner):
        result = runner.invoke(main, ["component", "install", "niquests", "--dry-run", "--json"])
        assert payload(result).get("restart_required") is None

    def test_status_says_what_ready_actually_means(self, runner: CliRunner, cfg: Path):
        """`adapters.ready` 探的是磁盘，不是服务端此刻的注册表。"""
        result = runner.invoke(main, ["status", "-c", str(cfg), "--json"])
        assert "重启" in payload(result)["adapters"]["note"]


class TestBodyPayload:
    """响应体怎么进 JSON。这几条直接决定调用方会不会把半截内容当成全文。"""

    def test_text_is_truncated_with_a_flag(self):
        from ipclick.cli.agent import _body_payload

        out = _body_payload(("x" * 500).encode(), 100)
        assert out["body_truncated"] is True
        assert len(out["body"]) == 100
        assert "-o" in out["body_note"]

    def test_short_text_is_not_flagged(self):
        from ipclick.cli.agent import _body_payload

        out = _body_payload("你好".encode(), 100)
        assert out == {"body": "你好", "body_encoding": "utf-8", "body_truncated": False}

    def test_small_binary_comes_back_whole_as_base64(self):
        """一张 2 KB 的 PNG 应该原样给出来，而不是因为"是二进制"就吞掉。"""
        import base64

        from ipclick.cli.agent import _body_payload

        blob = bytes(range(256)) * 4
        out = _body_payload(blob, 64 * 1024)
        assert out["body_encoding"] == "base64"
        assert out["body_truncated"] is False
        assert base64.b64decode(out["body"]) == blob

    def test_oversized_binary_gives_nothing_rather_than_garbage(self):
        """半截 base64 解不出任何东西——发出去只会让调用方去 decode 然后炸掉。"""
        from ipclick.cli.agent import _body_payload

        out = _body_payload(bytes(range(256)) * 100, 100)
        assert out["body"] == ""
        assert out["body_truncated"] is True
        assert "-o" in out["body_note"]

    def test_no_limit_means_no_truncation(self):
        from ipclick.cli.agent import _body_payload

        assert _body_payload(("x" * 10_000).encode(), 0)["body_truncated"] is False
        assert _body_payload(bytes(range(256)) * 100, 0)["body_truncated"] is False


class TestBadAdapterStillEmitsJson:
    """``--json`` 承诺的是"stdout 上有且只有一个 JSON 文档，成功失败都是"。

    ``-a 瞎写`` 曾经走 ``click.UsageError``：click 把错误打到 stderr 直接退出，
    stdout 一个字都没有。调用方（尤其是 AI）拿到空 stdout 只能靠猜——而这一条
    正是 SKILL.md 里让 AI"``ipclick ... --json | jq`` 永远安全"的依据。

    退出码保持 2 不变：这个名字是本地校验掉的，没联系过服务端，属于"命令行
    参数写错了"。5 留给"适配器存在但没装"（服务端回 FAILED_PRECONDITION）。
    """

    def test_stdout_carries_one_json_document(self, runner: CliRunner):
        result = runner.invoke(main, ["fetch", "https://example.com", "-a", "nosuchadapter", "--json"])
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert payload["exit_code"] == int(Exit.USAGE)
        assert "nosuchadapter" in payload["error"]

    def test_exit_code_stays_usage(self, runner: CliRunner):
        result = runner.invoke(main, ["fetch", "https://example.com", "-a", "nosuchadapter", "--json"])
        assert result.exit_code == int(Exit.USAGE)

    def test_plain_mode_still_says_why(self, runner: CliRunner):
        result = runner.invoke(main, ["fetch", "https://example.com", "-a", "nosuchadapter"])
        assert result.exit_code == int(Exit.USAGE)
        assert "nosuchadapter" in result.output


class TestStatusReportsWebHonestly:
    """`status` 是**另一个进程**，只看得到配置文件。

    `ipclick run -w` 用命令行打开 Web 端时并不改文件，于是文件里写着 false 而
    Web 端正开着。0.5.0 之前这一项叫 `web_enabled`，读的却是文件——名字让人
    （和 AI）以为它是运行状态，进而得出"Web 端没开"的错误结论。
    """

    def test_config_field_says_it_is_from_config(self, runner: CliRunner, cfg: Path):
        result = runner.invoke(main, ["status", "-c", str(cfg), "--json"])
        server = payload(result)["server"]
        assert "web_enabled_in_config" in server
        assert "web_enabled" not in server, "这个名字会被当成运行状态"

    def test_reachable_is_a_real_probe_not_a_config_read(self, runner: CliRunner, cfg: Path):
        """运行状态得真去连一下，不能靠读文件猜。"""
        result = runner.invoke(main, ["status", "-c", str(cfg), "--json"])
        assert isinstance(payload(result)["server"]["web_reachable"], bool)

    def test_port_open_matches_reality(self):
        import socket

        from ipclick.cli.agent import _port_open

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            port = sock.getsockname()[1]
            assert _port_open("127.0.0.1", port) is True
        # 出了 with 之后端口就关了
        assert _port_open("127.0.0.1", port) is False
