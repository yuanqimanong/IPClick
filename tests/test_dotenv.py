""".env 加载与配置优先级。

优先级是这套东西最容易出错、也最难排查的部分：配置"没生效"时，人第一反应
永远是怀疑自己写错了值，而不是怀疑被另一层盖掉了。所以逐层验。
"""

import os
from pathlib import Path

import pytest

from ipclick.config_loader.dotenv import find_env_file, load_dotenv, parse_env
from ipclick.config_loader.loader import ENV_OVERRIDES, example_config, example_env, load_config


class TestParse:
    def test_basic(self):
        assert parse_env("A=1\nB=two") == {"A": "1", "B": "two"}

    def test_export_prefix(self):
        assert parse_env("export A=1") == {"A": "1"}

    def test_comments_and_blank_lines(self):
        assert parse_env("# c\n\nA=1\n   \n# another") == {"A": "1"}

    def test_inline_comment_needs_whitespace(self):
        """`a#b` 里的 # 是值的一部分；`a  # x` 里的才是注释。"""
        assert parse_env("A=a#b") == {"A": "a#b"}
        assert parse_env("A=a  # 注释") == {"A": "a"}

    def test_double_quotes_keep_spaces_and_unescape(self):
        assert parse_env('A=" hi there "') == {"A": " hi there "}
        assert parse_env('A="line1\\nline2"') == {"A": "line1\nline2"}
        assert parse_env('A="say \\"hi\\""') == {"A": 'say "hi"'}

    def test_single_quotes_are_literal(self):
        """单引号里不做转义，和 shell 一致。"""
        assert parse_env(r"A='line1\nline2'") == {"A": r"line1\nline2"}

    def test_hash_inside_quotes_is_kept(self):
        assert parse_env('A="a # b"') == {"A": "a # b"}

    def test_value_can_contain_equals(self):
        """token 之类的值里带 = 很常见（base64 padding）。"""
        assert parse_env("A=abc=def==") == {"A": "abc=def=="}

    def test_empty_value(self):
        assert parse_env("A=") == {"A": ""}

    def test_malformed_lines_skipped(self):
        assert parse_env("no_equals_here\nA=1\n=novalue") == {"A": "1"}


class TestFindAndLoad:
    def test_finds_in_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        (tmp_path / ".env").write_text("A=1")
        monkeypatch.chdir(tmp_path)
        assert find_env_file() == tmp_path / ".env"

    def test_does_not_search_upwards(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """向上递归会让"在子目录里跑命令"意外加载到别处的 .env，
        而配置来源不明确是最难排查的一类问题。"""
        (tmp_path / ".env").write_text("A=1")
        sub = tmp_path / "sub"
        sub.mkdir()
        monkeypatch.chdir(sub)
        assert find_env_file() is None

    def test_missing_file_is_not_an_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        assert load_dotenv() == {}

    def test_injects_into_environ(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        (tmp_path / ".env").write_text("IPCLICK_TEST_KEY=from-dotenv")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("IPCLICK_TEST_KEY", raising=False)
        assert load_dotenv() == {"IPCLICK_TEST_KEY": "from-dotenv"}
        assert os.environ["IPCLICK_TEST_KEY"] == "from-dotenv"

    def test_does_not_override_real_env_var(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """核心约定：容器编排 / CI / systemd 注入的变量必须能压过仓库里的 .env。

        反过来的话，部署环境会被开发默认值悄悄改掉——而且没有任何提示。
        """
        (tmp_path / ".env").write_text("IPCLICK_TEST_KEY=from-dotenv")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("IPCLICK_TEST_KEY", "from-real-env")
        assert load_dotenv() == {}
        assert os.environ["IPCLICK_TEST_KEY"] == "from-real-env"

    def test_override_flag(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        (tmp_path / ".env").write_text("IPCLICK_TEST_KEY=from-dotenv")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("IPCLICK_TEST_KEY", "from-real-env")
        load_dotenv(override=True)
        assert os.environ["IPCLICK_TEST_KEY"] == "from-dotenv"

    def test_unreadable_file_does_not_raise(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """.env 是可选的便利设施，权限问题不该让整个服务起不来。"""
        env = tmp_path / ".env"
        env.write_text("A=1")
        env.chmod(0o000)
        monkeypatch.chdir(tmp_path)
        try:
            assert load_dotenv() == {}
        finally:
            env.chmod(0o644)


class TestConfigPrecedence:
    @pytest.fixture(autouse=True)
    def _clear(self):
        """清缓存 + 还原环境变量。

        load_dotenv 是**直接写 os.environ** 的（dotenv 的标准行为），
        monkeypatch 看不见这类写入，不还原的话上一个用例注入的 IPCLICK_HOST
        会漏到下一个用例里——第一次写这组测试就是这么翻的。
        """
        load_config.cache_clear()
        snapshot = dict(os.environ)
        yield
        os.environ.clear()
        os.environ.update(snapshot)
        load_config.cache_clear()

    def test_dotenv_reaches_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        (tmp_path / ".env").write_text("IPCLICK_HOST=10.1.1.1\nIPCLICK_PORT=12345\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("IPCLICK_HOST", raising=False)
        monkeypatch.delenv("IPCLICK_PORT", raising=False)
        cfg = load_config()
        assert cfg["SERVER"]["host"] == "10.1.1.1"
        assert cfg["SERVER"]["port"] == 12345

    def test_real_env_beats_dotenv(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        (tmp_path / ".env").write_text("IPCLICK_HOST=10.1.1.1\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("IPCLICK_HOST", "10.2.2.2")
        assert load_config()["SERVER"]["host"] == "10.2.2.2"

    def test_env_beats_config_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        cfg_file = tmp_path / "ipclick.toml"
        cfg_file.write_text('[SERVER]\nhost = "1.1.1.1"\nport = 1111\n', encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("IPCLICK_PORT", "2222")
        cfg = load_config(str(cfg_file))
        assert cfg["SERVER"]["host"] == "1.1.1.1", "没被环境变量覆盖的键要保留"
        assert cfg["SERVER"]["port"] == 2222

    def test_port_is_coerced_to_int(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("IPCLICK_PORT", "9999")
        assert load_config()["SERVER"]["port"] == 9999

    def test_bad_env_value_is_ignored_not_fatal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """环境变量写错了不该让服务起不来，但也不能变成 0 端口。"""
        monkeypatch.chdir(tmp_path)
        from ipclick.ports import DEFAULT_GRPC_PORT

        monkeypatch.setenv("IPCLICK_PORT", "not-a-number")
        assert load_config()["SERVER"]["port"] == DEFAULT_GRPC_PORT

    def test_empty_env_value_is_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """`IPCLICK_HOST=` 是"没设"，不是"设成空字符串"。"""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("IPCLICK_HOST", "")
        assert load_config()["SERVER"]["host"] == "[::]"

    def test_mode_and_log_level_overridable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("IPCLICK_MODE", "auto")
        monkeypatch.setenv("IPCLICK_LOG_LEVEL", "debug")
        cfg = load_config()
        assert cfg["GENERAL"]["mode"] == "auto"
        assert cfg["LOG"]["level"] == "debug"

    def test_every_documented_override_is_reachable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """表里列出来的每一项都必须真能落到配置上。

        散着写 os.getenv 的话，"到底哪些环境变量有用"只能靠翻代码，
        文档也必然和实现失步——所以有了这张表，也得有这条测试盯着它。
        """
        monkeypatch.chdir(tmp_path)
        for name, (section, key, caster) in ENV_OVERRIDES.items():
            load_config.cache_clear()
            probe = "7" if caster is int else "probe-value"
            monkeypatch.setenv(name, probe)
            cfg = load_config()
            assert cfg[section][key] == caster(probe), f"{name} 没有生效"
            monkeypatch.delenv(name)


class TestExampleEnv:
    """`.env` 模板现在只列机密——内容由 ipclick.secrets 生成，
    完整断言见 test_secrets.py。这里只验它确实还是个能解析的 .env。"""

    def test_parses_as_dotenv(self):
        parsed = parse_env(example_env())
        assert parsed, "模板应当至少有一项"
        assert all(v == "" for v in parsed.values())

    def test_dropping_it_in_changes_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """核心保证：把模板原样存成 .env，配置应与没有它时完全一致。"""
        monkeypatch.chdir(tmp_path)
        load_config.cache_clear()
        baseline = dict(load_config()["SERVER"])

        (tmp_path / ".env").write_text(example_env(), encoding="utf-8")
        load_config.cache_clear()
        assert dict(load_config()["SERVER"]) == baseline


class TestExampleConfig:
    def test_is_valid_toml(self):
        import tomllib

        tomllib.loads(example_config())

    def test_contains_all_sections(self):
        import tomllib

        parsed = tomllib.loads(example_config())
        for section in ("GENERAL", "CLIENT", "SERVER", "SECURITY", "DOWNLOADER", "BROWSER", "MONITOR", "LOG"):
            assert section in parsed, f"配置模板缺少 [{section}]"

    def test_keeps_comments(self):
        """模板的价值一大半在注释上——被剥掉就只剩一堆没头没尾的键。"""
        assert "#" in example_config()


class TestPerPortConfigFiles:
    """同一台机器上起多个实例：`--port 8001` 与 `--port 8002` 各读各的配置。

    0.4 只能靠 -c 一个个指过去，而那要求每次启动都记得带上——漏一次的症状是
    两个实例共用一份配置、往同一个 trace 库里写，界面上完全看不出来。
    """

    @pytest.fixture(autouse=True)
    def _clear(self):
        load_config.cache_clear()
        yield
        load_config.cache_clear()

    def test_port_specific_file_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "ipclick.toml").write_text("[SERVER]\nmax_workers = 10\n", encoding="utf-8")
        (tmp_path / "ipclick-8001.toml").write_text("[SERVER]\nmax_workers = 77\n", encoding="utf-8")
        assert load_config(None, 8001)["SERVER"]["max_workers"] == 77

    def test_falls_back_to_the_plain_name(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """单实例部署一个字都不用改。"""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "ipclick.toml").write_text("[SERVER]\nmax_workers = 10\n", encoding="utf-8")
        assert load_config(None, 9999)["SERVER"]["max_workers"] == 10

    def test_no_port_ignores_the_port_specific_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "ipclick.toml").write_text("[SERVER]\nmax_workers = 10\n", encoding="utf-8")
        (tmp_path / "ipclick-8001.toml").write_text("[SERVER]\nmax_workers = 77\n", encoding="utf-8")
        assert load_config()["SERVER"]["max_workers"] == 10

    def test_candidate_order(self):
        from ipclick.config_loader.loader import candidate_names

        assert candidate_names(8001)[0] == "ipclick-8001.toml"
        assert candidate_names(None) == ["ipclick.toml", ".ipclick.toml"]

    def test_writer_targets_the_same_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """页面写回的必须是进程实际读的那一个，否则就成了"改了没反应"。"""
        from ipclick.config_loader.writer import target_path

        monkeypatch.chdir(tmp_path)
        (tmp_path / "ipclick-8001.toml").write_text("[SERVER]\n", encoding="utf-8")
        assert target_path(None, 8001).name == "ipclick-8001.toml"
        assert target_path(None, None).name == "ipclick.toml"
