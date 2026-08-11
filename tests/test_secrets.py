"""机密的归属与解析。

规则：机密归 `.env` / 环境变量，行为配置归 `ipclick.toml`。写在 toml 里仍然
生效（受信环境图省事是合理的），但会被点名。
"""

import os
from pathlib import Path

import pytest

from ipclick.secrets import (
    SECRETS,
    SUPPRESS_KEY,
    audit,
    describe_source,
    env_template,
    proxy_config,
    resolve,
    warn_secrets_in_config,
)
from ipclick.utils.config_util import Settings


def _spec(env: str):
    return next(s for s in SECRETS if s.env == env)


@pytest.fixture(autouse=True)
def _clean_env():
    """机密相关的环境变量在用例间必须干净——漏一个就会让整组结果不可信。"""
    snapshot = dict(os.environ)
    for spec in SECRETS:
        os.environ.pop(spec.env, None)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


class TestRegistry:
    def test_covers_every_secret_in_shipped_config(self):
        """随包配置里不该再出现任何机密键——都该已经挪进这张表了。"""
        from ipclick.config_loader.loader import DEFAULT_CONFIG_PATH

        text = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
        for key in ("auth_key =", "auth_password =", "password =", "username ="):
            assert key not in text, f"随包配置里还留着机密键: {key}"

    def test_auth_token_not_in_shipped_config(self):
        from ipclick.config_loader.loader import DEFAULT_CONFIG_PATH

        lines = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8").splitlines()
        assert not [ln for ln in lines if ln.strip().startswith("auth_token =")]

    def test_env_names_are_unique(self):
        names = [s.env for s in SECRETS]
        assert len(names) == len(set(names))


class TestResolve:
    def test_unset(self):
        assert resolve(Settings({}), _spec("IPCLICK_AUTH_TOKEN")) == (None, "unset")

    def test_from_config(self):
        config = Settings({"SECURITY": {"auth_token": "t"}})
        assert resolve(config, _spec("IPCLICK_AUTH_TOKEN")) == ("t", "config")

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("IPCLICK_AUTH_TOKEN", "from-env")
        assert resolve(Settings({}), _spec("IPCLICK_AUTH_TOKEN")) == ("from-env", "env")

    def test_env_beats_config(self, monkeypatch: pytest.MonkeyPatch):
        """部署环境注入的必须能压过仓库里那份配置文件。"""
        monkeypatch.setenv("IPCLICK_AUTH_TOKEN", "from-env")
        config = Settings({"SECURITY": {"auth_token": "from-config"}})
        assert resolve(config, _spec("IPCLICK_AUTH_TOKEN"))[0] == "from-env"

    def test_nested_section(self):
        """redis_url 在子表 [DOWNLOADER.rate_limit] 里，取值要能钻进去。"""
        config = Settings({"DOWNLOADER": {"rate_limit": {"redis_url": "redis://x"}}})
        assert resolve(config, _spec("IPCLICK_REDIS_URL")) == ("redis://x", "config")

    def test_blank_config_value_is_unset(self):
        config = Settings({"SECURITY": {"auth_token": "   "}})
        assert resolve(config, _spec("IPCLICK_AUTH_TOKEN"))[1] == "unset"

    def test_list_token_reports_config(self):
        """轮换期间的多令牌只能写配置文件，来源要能正确识别。"""
        config = Settings({"SECURITY": {"auth_token": ["new", "old"]}})
        assert resolve(config, _spec("IPCLICK_AUTH_TOKEN"))[1] == "config"

    def test_audit_covers_all(self):
        assert len(audit(Settings({}))) == len(SECRETS)


class TestWarning:
    def test_silent_when_clean(self):
        assert warn_secrets_in_config(Settings({})) == []

    def test_flags_secrets_in_config(self):
        config = Settings({"SECURITY": {"auth_token": "t"}, "WEB": {"password": "p"}})
        found = {s.env for s in warn_secrets_in_config(config)}
        assert found == {"IPCLICK_AUTH_TOKEN", "IPCLICK_WEB_PASSWORD"}

    def test_still_works_when_suppressed(self, tmp_path: Path):
        """抑制的只是那行提示，机密本身照常生效——这是"受信环境"的用法。"""
        config = Settings({"SECURITY": {"auth_token": "t", SUPPRESS_KEY: True}})
        assert [s.env for s in warn_secrets_in_config(config)] == ["IPCLICK_AUTH_TOKEN"]
        assert resolve(config, _spec("IPCLICK_AUTH_TOKEN")) == ("t", "config")

    def test_warning_text_names_the_env_var(self):
        """光说"有机密"没用，得告诉人该挪到哪个环境变量。"""
        from loguru import logger

        messages: list[str] = []
        sink = logger.add(lambda m: messages.append(str(m)), level="WARNING")
        try:
            warn_secrets_in_config(Settings({"SECURITY": {"auth_token": "t"}}))
        finally:
            logger.remove(sink)
        assert any("IPCLICK_AUTH_TOKEN" in m for m in messages)

    def test_plain_redis_url_is_not_flagged(self):
        """默认那个不含凭据的本地地址写在配置里没问题——
        误报会让人对真正的告警脱敏。"""
        config = Settings({"DOWNLOADER": {"rate_limit": {"redis_url": "redis://127.0.0.1:6379/0"}}})
        assert warn_secrets_in_config(config) == []

    def test_redis_url_with_credentials_is_flagged(self):
        config = Settings({"DOWNLOADER": {"rate_limit": {"redis_url": "redis://user:pw@host:6379/0"}}})
        assert [s.env for s in warn_secrets_in_config(config)] == ["IPCLICK_REDIS_URL"]


class TestDescribeSource:
    def test_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("IPCLICK_AUTH_TOKEN", "t")
        assert "环境变量" in describe_source(Settings({}), _spec("IPCLICK_AUTH_TOKEN"))

    def test_config_is_marked(self):
        config = Settings({"SECURITY": {"auth_token": "t"}})
        assert "⚠️" in describe_source(config, _spec("IPCLICK_AUTH_TOKEN"))

    def test_plain_redis_url_not_marked(self):
        config = Settings({"DOWNLOADER": {"rate_limit": {"redis_url": "redis://127.0.0.1:6379/0"}}})
        assert "⚠️" not in describe_source(config, _spec("IPCLICK_REDIS_URL"))

    def test_unset(self):
        assert describe_source(Settings({}), _spec("IPCLICK_WEB_PASSWORD")) == "未配置"


class TestProxyConfig:
    def test_env_fills_credentials(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("IPCLICK_PROXY_AUTH_KEY", "user")
        monkeypatch.setenv("IPCLICK_PROXY_AUTH_PASSWORD", "pw")
        merged = proxy_config(Settings({"PROXY": {"host": "p.example.com", "port": 8080}}))
        assert merged["auth_key"] == "user"
        assert merged["auth_password"] == "pw"
        assert merged["host"] == "p.example.com", "非机密项要原样保留"

    def test_env_overrides_config(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("IPCLICK_PROXY_AUTH_PASSWORD", "from-env")
        merged = proxy_config(Settings({"PROXY": {"auth_password": "from-config"}}))
        assert merged["auth_password"] == "from-env"

    def test_config_still_works(self):
        merged = proxy_config(Settings({"PROXY": {"auth_key": "u", "auth_password": "p"}}))
        assert (merged["auth_key"], merged["auth_password"]) == ("u", "p")

    def test_reaches_the_proxy_url(self, monkeypatch: pytest.MonkeyPatch):
        """端到端：环境变量里的代理凭据要真的出现在最终的代理 URL 里。"""
        from ipclick.dto.models import ProxyConfig

        monkeypatch.setenv("IPCLICK_PROXY_AUTH_KEY", "u")
        monkeypatch.setenv("IPCLICK_PROXY_AUTH_PASSWORD", "p")
        config = Settings({"PROXY": {"scheme": "http", "host": "h", "port": 1}})
        url = ProxyConfig(**proxy_config(config)).to_url()
        assert url == "http://u:p@h:1"


class TestEnvTemplate:
    def test_lists_every_secret(self):
        text = env_template()
        for spec in SECRETS:
            assert f"{spec.env}=" in text

    def test_contains_no_deployment_params(self):
        """部署参数是给容器编排注入的，混进这个放密钥的文件只会让它变臃肿。"""
        text = env_template()
        for name in ("IPCLICK_HOST", "IPCLICK_PORT", "IPCLICK_MAX_WORKERS", "IPCLICK_MODE", "IPCLICK_LOG_LEVEL"):
            assert f"\n{name}=" not in text, f"{name} 不该出现在 .env 模板里"

    def test_all_values_empty(self):
        for line in env_template().splitlines():
            if line and not line.startswith("#"):
                assert line.endswith("="), f"模板里有预填值: {line!r}"

    def test_says_what_belongs_where(self):
        text = env_template()
        assert "只放机密" in text
        assert "ipclick.toml" in text
