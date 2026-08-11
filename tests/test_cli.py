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
        assert "拦截内网地址: True" in result.output

    def test_auth_token_value_not_printed(self, runner: CliRunner, tmp_path: Path):
        """只说有没有配，绝不打印令牌本身。"""
        cfg = tmp_path / "c.toml"
        cfg.write_text('[SECURITY]\nauth_token = "sup3r-s3cret-token"\n', encoding="utf-8")
        result = runner.invoke(main, ["config-info", "--config", str(cfg)])
        assert result.exit_code == 0
        assert "sup3r-s3cret-token" not in result.output
        assert "令牌鉴权:     已配置" in result.output

    def test_warns_when_no_auth(self, runner: CliRunner, tmp_path: Path):
        cfg = tmp_path / "c.toml"
        cfg.write_text("[SERVER]\nport = 1\n", encoding="utf-8")
        result = runner.invoke(main, ["config-info", "--config", str(cfg)])
        assert "任何人都能调用" in result.output

    def test_shows_tls_state(self, runner: CliRunner, tmp_path: Path):
        """TLS 配错了不会报错，只会悄悄少一层防护——必须能一眼看到。"""
        cfg = tmp_path / "c.toml"
        cfg.write_text(
            '[SECURITY.tls]\nenabled = true\ncert_file = "/c"\nkey_file = "/k"\n'
            'ca_file = "/ca"\nrequire_client_cert = true\n',
            encoding="utf-8",
        )
        result = runner.invoke(main, ["config-info", "--config", str(cfg)])
        assert "mTLS" in result.output

    def test_shows_host_limits(self, runner: CliRunner, tmp_path: Path):
        cfg = tmp_path / "c.toml"
        cfg.write_text(
            "[DOWNLOADER.concurrency]\nper_host_max_concurrent = 7\n"
            '\n[DOWNLOADER.rate_limit]\nper_host_qps = 3\nbackend = "redis"\n',
            encoding="utf-8",
        )
        result = runner.invoke(main, ["config-info", "--config", str(cfg)])
        assert "并发上限:     7" in result.output
        assert "集群共享" in result.output

    def test_limits_off_by_default(self, runner: CliRunner, tmp_path: Path):
        cfg = tmp_path / "c.toml"
        cfg.write_text("[SERVER]\nport = 1\n", encoding="utf-8")
        result = runner.invoke(main, ["config-info", "--config", str(cfg)])
        assert "Per-host limits:\n  未启用" in result.output

    def test_shows_resolved_browser_engine(self, runner: CliRunner, tmp_path: Path):
        """engine = auto 时要显示**实际解析到**的引擎，不然等于没说。"""
        cfg = tmp_path / "c.toml"
        cfg.write_text('[BROWSER]\nengine = "playwright"\n', encoding="utf-8")
        result = runner.invoke(main, ["config-info", "--config", str(cfg)])
        assert "playwright" in result.output

    def test_bad_engine_reported_not_crashed(self, runner: CliRunner, tmp_path: Path):
        """配置写错了要说清楚，而不是让整条命令挂掉。"""
        cfg = tmp_path / "c.toml"
        cfg.write_text('[BROWSER]\nengine = "netscape"\n', encoding="utf-8")
        result = runner.invoke(main, ["config-info", "--config", str(cfg)])
        assert result.exit_code == 0
        assert "配置错误" in result.output

    def test_shows_client_mode(self, runner: CliRunner, tmp_path: Path):
        cfg = tmp_path / "c.toml"
        cfg.write_text(
            '[GENERAL]\nmode = "cluster"\n\n[CLUSTER]\nnodes = [{ id = "a", address = "h:1" }]\n',
            encoding="utf-8",
        )
        result = runner.invoke(main, ["config-info", "--config", str(cfg)])
        assert "运行模式:     cluster" in result.output
        assert "集群节点:     1 个" in result.output

    def test_works_without_config_file(self, runner: CliRunner):
        result = runner.invoke(main, ["config-info"])
        assert result.exit_code == 0
        assert "Current configuration" in result.output


class TestExampleFlag:
    """ipclick --example / -e：输出配置模板。"""

    def test_outputs_valid_toml(self, runner: CliRunner):
        import tomllib

        result = runner.invoke(main, ["--example"])
        assert result.exit_code == 0
        tomllib.loads(result.output)

    def test_short_flag(self, runner: CliRunner):
        assert runner.invoke(main, ["-e"]).output == runner.invoke(main, ["--example"]).output

    def test_output_is_redirectable(self, runner: CliRunner):
        """`ipclick -e > ipclick.toml` 出来的必须是能直接用的文件——
        不能带任何日志前缀或提示语。"""
        output = runner.invoke(main, ["--example"]).output
        assert output.lstrip().startswith("#"), f"模板开头混进了别的东西: {output[:60]!r}"
        assert "Starting" not in output

    def test_keeps_comments(self):
        """模板的价值一大半在注释上。"""
        from ipclick.config_loader.loader import example_config

        assert example_config().count("#") > 30

    def test_env_template(self, runner: CliRunner):
        result = runner.invoke(main, ["-e", "env"])
        assert result.exit_code == 0
        assert "IPCLICK_HOST=" in result.output
        assert "IPCLICK_WEB_PASSWORD=" in result.output

    def test_toml_is_the_default_format(self, runner: CliRunner):
        assert runner.invoke(main, ["-e"]).output == runner.invoke(main, ["-e", "toml"]).output

    def test_env_and_toml_differ(self, runner: CliRunner):
        assert runner.invoke(main, ["-e", "env"]).output != runner.invoke(main, ["-e", "toml"]).output

    def test_unknown_format_rejected(self, runner: CliRunner):
        result = runner.invoke(main, ["-e", "yaml"])
        assert result.exit_code != 0

    def test_bare_invocation_shows_help(self, runner: CliRunner):
        """不带子命令时给帮助，而不是静默退出。"""
        result = runner.invoke(main, [])
        assert result.exit_code == 0
        assert "config-info" in result.output
