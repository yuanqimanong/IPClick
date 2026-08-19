from __future__ import annotations

import pytest

from ipclick.exceptions import URLNotAllowedError
from ipclick.utils.url_util import DEFAULT_ALLOWED_SCHEMES, URLPolicy, merge_query_params, validate_url


def test_default_policy_blocks_metadata_but_allows_private() -> None:
    policy = URLPolicy.from_config({})
    assert policy.allowed_schemes == DEFAULT_ALLOWED_SCHEMES
    assert policy.block_metadata_endpoints is True
    assert policy.block_private_networks is False

    validate_url("http://127.0.0.1:8080/x", policy)
    with pytest.raises(URLNotAllowedError, match="云元数据"):
        validate_url("http://169.254.169.254/latest/meta-data/", policy)


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
