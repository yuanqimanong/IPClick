from __future__ import annotations

import pytest

from ipclick.exceptions import HostResolutionError, URLNotAllowedError
from ipclick.utils import url_util
from ipclick.utils.url_util import DEFAULT_ALLOWED_SCHEMES, URLPolicy, merge_query_params, validate_url


def test_default_policy_blocks_metadata_but_allows_private() -> None:
    policy = URLPolicy.from_config({})
    assert policy.allowed_schemes == DEFAULT_ALLOWED_SCHEMES
    assert policy.block_metadata_endpoints is True
    assert policy.block_private_networks is False

    validate_url("http://127.0.0.1:8080/x", policy)
    with pytest.raises(URLNotAllowedError, match="云元数据"):
        validate_url("http://169.254.169.254/latest/meta-data/", policy)
    with pytest.raises(URLNotAllowedError, match="云元数据"):
        validate_url("http://[::ffff:169.254.169.254]/latest/meta-data/", policy)


@pytest.mark.parametrize("url", ["ftp://example.com", "gopher://example.com"])
def test_scheme_outside_the_allowlist_is_refused(url: str) -> None:
    with pytest.raises(URLNotAllowedError, match="不允许的协议"):
        validate_url(url, URLPolicy())


def test_missing_host_is_refused() -> None:
    with pytest.raises(URLNotAllowedError, match="缺少主机名"):
        validate_url("http:///just/a/path", URLPolicy())


def test_private_addresses_are_refused_when_enabled() -> None:
    policy = URLPolicy.from_config({"block_private_networks": True})
    for url in ("http://127.0.0.1/x", "http://10.0.0.5/x", "http://[::1]/x", "http://192.168.1.1/x"):
        with pytest.raises(URLNotAllowedError, match="内网地址"):
            validate_url(url, policy)


@pytest.mark.parametrize(
    "url",
    [
        # AWS ECS/Fargate 的任务元数据端点，就是发任务角色临时凭据的地方
        "http://169.254.170.2/v2/credentials/abc",
        "http://169.254.170.23/latest/meta-data/iam/security-credentials/",
        # 同段里其余厂商/用途的端点
        "http://169.254.169.253/",
        # IPv4-mapped 写法不能绕过
        "http://[::ffff:169.254.170.2]/v2/credentials/abc",
        # 链路本地的 IPv6 侧
        "http://[fe80::1]/",
    ],
)
def test_metadata_blocking_covers_the_whole_link_local_range(url: str) -> None:
    """``block_metadata_endpoints`` 不能只认 169.254.169.254 一个字面量。

    默认配置是 ``block_private_networks = false`` + ``block_metadata_endpoints = true``。
    只列举字面量的话，一次 302 跳到 ``http://169.254.170.2/v2/credentials/...`` 就是放行的
    ——那是 AWS ECS/Fargate 发任务角色临时凭据的端点，正是这个开关要防的东西。
    云厂商的端点全在链路本地段里，而链路本地地址对下载服务从来不是合法目标，整段拒掉。
    """
    policy = URLPolicy.from_config({"block_metadata_endpoints": True, "block_private_networks": False})
    with pytest.raises(URLNotAllowedError):
        validate_url(url, policy)


def test_non_link_local_metadata_endpoints_are_still_listed_literally() -> None:
    """阿里云在 CGNAT 段、AWS 的 IPv6 IMDS 在 ULA 段，都不在链路本地里。"""
    policy = URLPolicy.from_config({"block_metadata_endpoints": True, "block_private_networks": False})
    for url in ("http://100.100.100.200/latest/meta-data/", "http://[fd00:ec2::254]/latest/meta-data/"):
        with pytest.raises(URLNotAllowedError):
            validate_url(url, policy)


def test_allowlist_wins_over_every_other_rule() -> None:
    policy = URLPolicy.from_config(
        {"block_private_networks": True, "block_metadata_endpoints": True, "allowlist": ["169.254.169.254"]}
    )
    validate_url("http://169.254.169.254/latest/", policy)


def test_public_address_passes_with_private_blocking_on() -> None:
    validate_url("http://8.8.8.8/x", URLPolicy.from_config({"block_private_networks": True}))


def test_checks_are_skipped_entirely_when_both_switches_are_off() -> None:
    policy = URLPolicy.from_config({"block_metadata_endpoints": False, "block_private_networks": False})
    validate_url("http://169.254.169.254/latest/", policy)


def test_string_boolean_security_switches_are_parsed_by_value() -> None:
    policy = URLPolicy.from_config({"block_metadata_endpoints": "false", "block_private_networks": "true"})

    assert policy.block_metadata_endpoints is False
    assert policy.block_private_networks is True


def test_dns_failure_is_rejected_when_ssrf_checks_are_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """解析不出来仍然必须拒绝——放行等于让 DNS 失败成为绕过准入的口子。

    但抛的是 HostResolutionError 而不是 URLNotAllowedError：解析失败是网络故障，
    不是"被策略拒绝"。服务端据此把它变成普通的失败响应（status_code == -1），
    与关闭准入时适配器自己解析失败的表现一致，也与 README 的承诺一致。
    两者的排查方向完全相反——一个查 DNS，一个去改 [SECURITY] 白名单。
    """
    monkeypatch.setattr(url_util, "_resolve_host", lambda _host: [])

    with pytest.raises(HostResolutionError, match="无法解析主机"):
        validate_url("https://temporarily-unresolved.example", URLPolicy())

    assert not issubclass(HostResolutionError, URLNotAllowedError)


def test_custom_scheme_allowlist_is_normalised() -> None:
    policy = URLPolicy.from_config({"allowed_schemes": ["HTTPS"]})
    assert policy.allowed_schemes == frozenset({"https"})
    with pytest.raises(URLNotAllowedError):
        validate_url("http://8.8.8.8/x", policy)


def test_merge_query_params() -> None:
    assert merge_query_params("http://e.com/p", None) == "http://e.com/p"
    assert merge_query_params("http://e.com/p", {"a": 1}) == "http://e.com/p?a=1"
    assert merge_query_params("http://e.com/p?x=1", {"a": 2}) == "http://e.com/p?x=1&a=2"
    assert merge_query_params("http://e.com/p", {"a": 1, "b": None}) == "http://e.com/p?a=1"
    assert merge_query_params("http://e.com/p?x=1", {"a": None}) == "http://e.com/p?x=1"
