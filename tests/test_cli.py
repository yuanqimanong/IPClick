"""CLI 行为。"""

from pathlib import Path

from click.testing import CliRunner
import pytest

from ipclick import __version__
from ipclick.cli.main import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestVersion:
    def test_version_matches_package(self, runner: CliRunner):
        """回归：CLI 里硬编码了 "0.1.3"，改版本号必然忘记同步。"""
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert __version__ in result.output


class TestRunOptions:
    def test_verbose_flag_exists(self, runner: CliRunner):
        """回归：README 写了 --verbose，代码里并没有。"""
        result = runner.invoke(main, ["run", "--help"])
        assert result.exit_code == 0
        assert "--verbose" in result.output

    def test_host_and_port_flags_exist(self, runner: CliRunner):
        result = runner.invoke(main, ["run", "--help"])
        assert "--host" in result.output
        assert "--port" in result.output

    def test_nonexistent_config_rejected(self, runner: CliRunner):
        result = runner.invoke(main, ["run", "--config", "/no/such/file.toml"])
        assert result.exit_code != 0


class TestConfigInfo:
    def test_reads_uppercase_sections(self, runner: CliRunner, tmp_path: Path):
        """回归：以前读的是小写 server/client/workers，永远取不到值，
        于是不管配置写了什么都打印同一串默认值。"""
        cfg = tmp_path / "c.toml"
        cfg.write_text(
            '[SERVER]\nhost = "10.0.0.1"\nport = 4321\nmax_workers = 77\n',
            encoding="utf-8",
        )

        result = runner.invoke(main, ["config-info", "--config", str(cfg)])
        assert result.exit_code == 0
        assert "10.0.0.1" in result.output
        assert "4321" in result.output
        assert "77" in result.output

    def test_proxy_password_not_printed(self, runner: CliRunner, tmp_path: Path):
        cfg = tmp_path / "c.toml"
        cfg.write_text(
            '[PROXY]\nhost = "p.example.com"\nport = 8080\nauth_key = "user"\nauth_password = "sup3rs3cret"\n',
            encoding="utf-8",
        )

        result = runner.invoke(main, ["config-info", "--config", str(cfg)])
        assert result.exit_code == 0
        assert "sup3rs3cret" not in result.output
        assert "p.example.com" in result.output

    def test_shows_security_setting(self, runner: CliRunner, tmp_path: Path):
        cfg = tmp_path / "c.toml"
        cfg.write_text("[SECURITY]\nblock_private_networks = true\n", encoding="utf-8")
        result = runner.invoke(main, ["config-info", "--config", str(cfg)])
        assert "Block private networks: True" in result.output

    def test_does_not_print_unimplemented_config(self, runner: CliRunner, tmp_path: Path):
        """[DOWNLOADER] 目前没有消费方，打印它会让人以为改了就生效。"""
        cfg = tmp_path / "c.toml"
        cfg.write_text("[DOWNLOADER]\nconnect_timeout = 999\ndownload_timeout = 888\n", encoding="utf-8")
        result = runner.invoke(main, ["config-info", "--config", str(cfg)])
        assert result.exit_code == 0
        assert "999" not in result.output
        assert "888" not in result.output

    def test_cluster_nodes_marked_unimplemented(self, runner: CliRunner):
        """默认配置里有一个预留节点，必须标注尚未实现。"""
        result = runner.invoke(main, ["config-info"])
        assert result.exit_code == 0
        if "Cluster nodes" in result.output:
            assert "尚未实现" in result.output

    def test_works_without_config_file(self, runner: CliRunner):
        result = runner.invoke(main, ["config-info"])
        assert result.exit_code == 0
        assert "Current configuration" in result.output
