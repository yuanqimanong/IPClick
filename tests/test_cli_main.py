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
