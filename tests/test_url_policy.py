"""服务端目标 URL 准入策略（SSRF 防护）。"""

import pytest

from ipclick.exceptions import URLNotAllowedError
from ipclick.utils.url_util import URLPolicy, validate_url


class TestSchemeAllowlist:
    @pytest.mark.parametrize("url", ["http://example.com", "https://example.com/a?b=1"])
    def test_http_and_https_allowed(self, url: str):
        validate_url(url, URLPolicy(block_metadata_endpoints=False))

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "gopher://127.0.0.1:11211/_stats",
            "ftp://example.com/x",
            "dict://127.0.0.1:6379/info",
        ],
    )
    def test_dangerous_schemes_blocked(self, url: str):
        with pytest.raises(URLNotAllowedError, match="不允许的协议"):
            validate_url(url)

    def test_missing_host_rejected(self):
        with pytest.raises(URLNotAllowedError, match="缺少主机名"):
            validate_url("http://")


class TestMetadataEndpoints:
    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",
            "http://100.100.100.200/latest/meta-data/",
        ],
    )
    def test_cloud_metadata_blocked_by_default(self, url: str):
        """云元数据地址没有任何正当的代理用途，默认就该拦掉。"""
        with pytest.raises(URLNotAllowedError, match="元数据"):
            validate_url(url)

    def test_can_be_disabled(self):
        policy = URLPolicy(block_metadata_endpoints=False, block_private_networks=False)
        validate_url("http://169.254.169.254/", policy)


class TestPrivateNetworks:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:8000/admin",
            "http://localhost:6379/",
            "http://10.0.0.5/",
            "http://192.168.1.1/",
            "http://172.16.0.1/",
            "http://[::1]:8080/",
        ],
    )
    def test_blocked_when_enabled(self, url: str):
        with pytest.raises(URLNotAllowedError, match="内网地址"):
            validate_url(url, URLPolicy(block_private_networks=True))

    @pytest.mark.parametrize("url", ["http://127.0.0.1:8000/", "http://192.168.1.1/"])
    def test_allowed_by_default_for_backwards_compat(self, url: str):
        """默认放行内网，避免破坏"本机服务端代理本机服务"的既有用法。"""
        validate_url(url, URLPolicy())

    def test_allowlist_overrides_block(self):
        policy = URLPolicy(block_private_networks=True, allowlist=frozenset({"127.0.0.1"}))
        validate_url("http://127.0.0.1:8000/", policy)

    def test_unresolvable_host_is_not_blocked_here(self):
        """DNS 解析不了就交给适配器报真实网络错误，安全层不越权。"""
        validate_url("http://no-such-host.invalid/", URLPolicy(block_private_networks=True))


class TestPolicyFromConfig:
    def test_defaults_when_section_absent(self):
        policy = URLPolicy.from_config(None)
        assert policy.allowed_schemes == frozenset({"http", "https"})
        assert policy.block_metadata_endpoints is True
        assert policy.block_private_networks is False

    def test_reads_config_values(self):
        policy = URLPolicy.from_config(
            {
                "allowed_schemes": ["HTTPS"],
                "block_private_networks": True,
                "allowlist": ["Internal.Example.COM"],
            }
        )
        assert policy.allowed_schemes == frozenset({"https"})
        assert policy.block_private_networks is True
        assert "internal.example.com" in policy.allowlist

    def test_https_only_policy_rejects_http(self):
        policy = URLPolicy.from_config({"allowed_schemes": ["https"]})
        with pytest.raises(URLNotAllowedError):
            validate_url("http://example.com", policy)
