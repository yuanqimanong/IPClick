from __future__ import annotations

from importlib import import_module
import json
import os
from pathlib import Path

from click.testing import CliRunner
import pytest

from ipclick.cli.main import main
from ipclick.dto.models import DownloadResponse
from ipclick.exceptions import ConfigError


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
def test_init_force_tightens_existing_env_permissions(tmp_path: Path) -> None:
    target = tmp_path / "project"
    target.mkdir()
    env_file = target / ".env"
    env_file.write_text("OLD=value\n", encoding="utf-8")
    env_file.chmod(0o644)

    result = CliRunner().invoke(main, ["init", "--force", "--dir", str(target)])

    assert result.exit_code == 0, result.output
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_config_info_resolves_port_placeholders(tmp_path: Path) -> None:
    config = tmp_path / "custom.toml"
    config.write_text(
        """
[SERVER]
port = 19123

[LOG]
output = "logs/ipclick-{port}.log"

[TRACE]
sqlite_enabled = true
sqlite_path = "data/trace-{port}.db"

[PROXY]
tunnel_server = "gateway.example:9000"
""".strip(),
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["config-info", "--config", str(config)])

    assert result.exit_code == 0, result.output
    assert "logs/ipclick-19123.log" in result.output
    assert "data/trace-19123.db" in result.output
    assert "代理:         gateway.example:9000" in result.output
    assert "gateway.example:9000:0" not in result.output


def test_status_uses_configured_ipv6_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "status.toml"
    config.write_text('[SERVER]\nhost = "2001:db8::1"\nport = 19123\n', encoding="utf-8")
    targets: list[str] = []

    health_kwargs: list[dict[str, object]] = []

    def fake_health(target: str, **kwargs: object) -> tuple[bool, str]:
        targets.append(target)
        health_kwargs.append(kwargs)
        return True, "SERVING"

    monkeypatch.setattr("ipclick.health.check_health", fake_health)
    monkeypatch.setattr("ipclick.cli.agent._port_open", lambda *_args, **_kwargs: False)

    result = CliRunner().invoke(main, ["status", "--config", str(config), "--json"])

    assert result.exit_code == 0, result.output
    assert targets == ["[2001:db8::1]:19123"]
    assert getattr(health_kwargs[0]["tls"], "enabled", None) is False
    assert json.loads(result.output)["server"]["target"] == "[2001:db8::1]:19123"


def test_health_formats_ipv6_target(monkeypatch: pytest.MonkeyPatch) -> None:
    targets: list[str] = []

    def fake_health(target: str, **_kwargs: object) -> tuple[bool, str]:
        targets.append(target)
        return True, "SERVING"

    cli_main_module = import_module("ipclick.cli.main")
    monkeypatch.setattr(cli_main_module, "check_health", fake_health)

    result = CliRunner().invoke(main, ["health", "--host", "::1", "--port", "19123"])

    assert result.exit_code == 0, result.output
    assert targets == ["[::1]:19123"]


def test_status_json_reports_invalid_web_port_as_one_json_document(tmp_path: Path) -> None:
    config = tmp_path / "invalid-status.toml"
    config.write_text('[SERVER]\nport = 19123\n[WEB]\nport = "bad"\n', encoding="utf-8")

    result = CliRunner().invoke(main, ["status", "--config", str(config), "--json"])

    assert result.exit_code == 5
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["exit_code"] == 5
    assert "状态配置无效" in payload["error"]


def test_health_passes_tls_settings_from_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "tls.toml"
    config.write_text("[SERVER]\nport = 19123\n[SECURITY.tls]\nenabled = true\n", encoding="utf-8")
    seen: list[object] = []

    def fake_health(_target: str, **kwargs: object) -> tuple[bool, str]:
        seen.append(kwargs["tls"])
        return True, "SERVING"

    cli_main_module = import_module("ipclick.cli.main")
    monkeypatch.setattr(cli_main_module, "check_health", fake_health)
    result = CliRunner().invoke(main, ["health", "--config", str(config)])

    assert result.exit_code == 0, result.output
    assert getattr(seen[0], "enabled", None) is True


def test_json_command_is_not_polluted_by_invalid_environment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IPCLICK_PORT", "not-a-port")

    result = CliRunner().invoke(main, ["config", "show", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True


def test_fetch_output_file_does_not_repeat_body_in_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    content = b"x" * 100_000

    class FakeDownloader:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def request(self, **kwargs: object) -> DownloadResponse:
            return DownloadResponse(url=str(kwargs["url"]), status_code=200, content=content, text=content.decode())

        def close(self) -> None:
            pass

    monkeypatch.setattr("ipclick.sdk.Downloader", FakeDownloader)
    output = tmp_path / "body.bin"

    result = CliRunner().invoke(main, ["fetch", "https://example.com", "--json", "-o", str(output)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["body_omitted"] is True
    assert "body" not in payload
    assert output.read_bytes() == content


def test_fetch_rejects_negative_max_body() -> None:
    result = CliRunner().invoke(main, ["fetch", "https://example.com", "--max-body", "-1"])

    assert result.exit_code == 2


def test_fetch_json_reports_downloader_construction_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenDownloader:
        def __init__(self, **_kwargs: object) -> None:
            raise ConfigError("invalid client configuration")

    monkeypatch.setattr("ipclick.sdk.Downloader", BrokenDownloader)

    result = CliRunner().invoke(main, ["fetch", "https://example.com", "--json"])

    assert result.exit_code == 5
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["exit_code"] == 5
    assert "invalid client configuration" in payload["error"]


@pytest.mark.parametrize("port", ["0", "65536", "70000", "75064", "-1"])
def test_health_rejects_out_of_range_ports(port: str) -> None:
    """超范围端口必须在参数层被拒，不能交给 gRPC 去 mod 65536 回绕。

    9528 + 65536 = 75064：回绕之后 health 连的其实是 9528，于是对着一个不可能
    存在的端口回答 SERVING、退出码 0。这比直接报错危险得多——运维照着它判断
    "服务在听"，而那台机器上根本没有这个端口。
    """
    result = CliRunner().invoke(main, ["health", "--port", port, "--timeout", "1"])

    assert result.exit_code == 2, result.output
    assert "1..65535" in result.output


@pytest.mark.parametrize("port", ["1", "65535"])
def test_health_accepts_the_boundary_ports(port: str, monkeypatch: pytest.MonkeyPatch) -> None:
    cli_main_module = import_module("ipclick.cli.main")
    monkeypatch.setattr(cli_main_module, "check_health", lambda *_a, **_k: (True, "SERVING"))

    result = CliRunner().invoke(main, ["health", "--port", port, "--timeout", "1"])

    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("port", ["-1", "0", "70000", "65536"])
def test_init_refuses_ports_the_loader_would_reject(port: str, tmp_path: Path) -> None:
    """init 不校验的话，会生成一份 ipclick 自己都加载不了的配置。

    --port 70000 生成的 toml 一喂回 config-info 就报"必须在 1..65535 范围内"；
    --port -1 更是生成出文件名带负号的 ipclick--1.toml；--port 0 则被真假值判断
    当成"没传"，文件名不带端口、端口没写进去、也没有任何提示。
    """
    result = CliRunner().invoke(main, ["init", "--dir", str(tmp_path), "--port", port])

    assert result.exit_code == 2, result.output
    assert "1<=x<=65535" in result.output
    assert list(tmp_path.iterdir()) == []


def test_init_says_it_aborted_rather_than_skipped(tmp_path: Path) -> None:
    """措辞要说实话：这里是整体中止，一个文件都不会生成。"""
    (tmp_path / ".env").write_text("", encoding="utf-8")

    result = CliRunner().invoke(main, ["init", "--dir", str(tmp_path)])

    assert result.exit_code == 1, result.output
    assert "已存在，中止" in result.output
    assert not (tmp_path / "ipclick.toml").exists()


@pytest.mark.parametrize("timeout", ["-5", "0"])
def test_health_refuses_a_non_positive_timeout(timeout: str) -> None:
    """--timeout -5 会让 gRPC 立刻 DEADLINE_EXCEEDED，把一个健康的服务端报成挂了。"""
    result = CliRunner().invoke(main, ["health", "--timeout", timeout])

    assert result.exit_code == 2, result.output
    assert "x>0" in result.output


def test_config_show_does_not_redact_a_boolean_switch(tmp_path: Path) -> None:
    """脱敏只按键名子串判断会误伤：allow_secrets_in_config 是开关不是机密。

    false 曾被渲染成空串，与"未配置"在输出里无法区分。机密一定是字符串，
    布尔和数字一律原样输出。
    """
    config = tmp_path / "redact.toml"
    config.write_text(
        '[SERVER]\nport = 19528\n[SECURITY]\nallow_secrets_in_config = false\nauth_token = "s3cr3t"\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["config", "show", "-c", str(config), "-s", "SECURITY", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["config"]["allow_secrets_in_config"] is False
    assert payload["config"]["auth_token"] == "<已配置>"


def test_config_show_flags_values_that_will_not_take_effect(tmp_path: Path) -> None:
    """config show 展示的是文件值；值其实起不来时必须明说。

    这个命令组自称"实际生效"，却曾对 max_workers = 0 照原样打印并退出 0，
    而 config-info 对同一个文件报错退出 1——两条命令给出相反结论。
    """
    config = tmp_path / "broken.toml"
    config.write_text("[SERVER]\nport = 19528\nmax_workers = 0\n", encoding="utf-8")

    result = CliRunner().invoke(main, ["config", "show", "-c", str(config), "--json"])

    assert result.exit_code == 0, result.output
    assert "max_workers" in json.loads(result.output)["invalid"]

    good = tmp_path / "fine.toml"
    good.write_text("[SERVER]\nport = 19528\n", encoding="utf-8")
    ok_result = CliRunner().invoke(main, ["config", "show", "-c", str(good), "--json"])
    assert json.loads(ok_result.output)["invalid"] is None


@pytest.mark.parametrize(
    "args",
    [
        ["fetch", "-J", "-H", "BAD", "http://127.0.0.1:9/"],
        ["fetch", "-J", "-X", "FROBNICATE", "http://127.0.0.1:9/"],
        ["fetch", "-J", "--max-body", "-1", "http://127.0.0.1:9/"],
        ["fetch", "-J"],
        ["nosuchcommand", "-J"],
    ],
)
def test_usage_errors_still_honour_the_json_contract(args: list[str]) -> None:
    """SKILL.md：加 --json 时 stdout 上有且只有一个 JSON 文档，成功失败都是。

    参数错误由 Click 在命令体运行**之前**抛出，原先走它自带的 usage 文本 + stderr，
    于是 stdout 是 0 字节、`ipclick ... --json | jq` 直接崩——而这正是契约里
    列为退出码 2 的那一类失败。
    """
    result = CliRunner().invoke(main, args)

    assert result.exit_code == 2, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["exit_code"] == 2
    assert payload["error"]


def test_non_json_usage_errors_keep_the_click_output() -> None:
    """不带 --json 时行为一个字都不变。"""
    result = CliRunner().invoke(main, ["fetch", "-H", "BAD", "http://127.0.0.1:9/"])

    assert result.exit_code == 2
    assert "Usage:" in result.output


@pytest.mark.parametrize("args", [["run", "--port", "0"], ["run", "--web-port", "0"], ["run", "--port", "70000"]])
def test_run_refuses_out_of_range_ports(args: list[str]) -> None:
    """--port 0 原先被真假值判断当成"没传"，超范围值一路走到绑定失败才在日志里报错。"""
    result = CliRunner().invoke(main, args)

    assert result.exit_code == 2, result.output
    assert "1<=x<=65535" in result.output


def test_config_info_reports_auth_that_comes_from_the_cluster_secret(tmp_path: Path) -> None:
    """配了共享密钥且能识别本节点 id 时，整个端口其实已经要求鉴权了。

    只看 [SECURITY].auth_token 的话会报"未配置（任何人都能调用）"——两个方向都误导：
    以为端口开着的其实锁着，以为什么都没变的其实普通调用方全线 UNAUTHENTICATED。
    """
    config = tmp_path / "cluster.toml"
    config.write_text('[SERVER]\nport = 19528\n[CLUSTER]\nself_id = "node-a"\n', encoding="utf-8")

    with_secret = CliRunner(env={"IPCLICK_CLUSTER_SECRET": "shared-secret"}).invoke(
        main, ["config-info", "-c", str(config)]
    )
    without = CliRunner(env={"IPCLICK_CLUSTER_SECRET": ""}).invoke(main, ["config-info", "-c", str(config)])

    assert "共享密钥派生" in with_secret.output, with_secret.output
    assert "未配置（任何人都能调用）" in without.output, without.output
