from dataclasses import dataclass, field
import ipaddress
import socket
from typing import Any
from urllib.parse import urlencode, urlparse, urlsplit, urlunparse

from ipclick.exceptions import URLNotAllowedError


DEFAULT_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

_METADATA_ADDRESSES: frozenset[str] = frozenset(
    {
        "169.254.169.254",
        "100.100.100.200",
        "fd00:ec2::254",
    }
)


def _is_metadata_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return str(ip) in _METADATA_ADDRESSES


def _is_private_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


@dataclass(frozen=True)
class URLPolicy:
    allowed_schemes: frozenset[str] = DEFAULT_ALLOWED_SCHEMES
    block_metadata_endpoints: bool = True
    block_private_networks: bool = False
    allowlist: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_config(cls, security_config: dict[str, object] | None) -> "URLPolicy":
        config = dict(security_config or {})

        schemes = config.get("allowed_schemes")
        allowed_schemes = (
            frozenset(str(s).lower() for s in schemes)
            if isinstance(schemes, (list, tuple, set)) and schemes
            else DEFAULT_ALLOWED_SCHEMES
        )

        allowlist_raw = config.get("allowlist")
        allowlist = (
            frozenset(str(h).lower() for h in allowlist_raw)
            if isinstance(allowlist_raw, (list, tuple, set))
            else frozenset()
        )

        return cls(
            allowed_schemes=allowed_schemes,
            block_metadata_endpoints=bool(config.get("block_metadata_endpoints", True)),
            block_private_networks=bool(config.get("block_private_networks", False)),
            allowlist=allowlist,
        )


def _resolve_host(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return []

    resolved: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        address = info[4][0]
        try:
            resolved.append(ipaddress.ip_address(address))
        except ValueError:
            continue
    return resolved


def validate_url(url: str, policy: URLPolicy | None = None) -> None:
    policy = policy or URLPolicy()

    try:
        parts = urlsplit(url)
    except ValueError as e:
        raise URLNotAllowedError(f"URL 解析失败: {e}") from e

    scheme = parts.scheme.lower()
    if scheme not in policy.allowed_schemes:
        allowed = ", ".join(sorted(policy.allowed_schemes))
        raise URLNotAllowedError(f"不允许的协议 {scheme!r}，仅支持: {allowed}")

    try:
        host = parts.hostname
    except ValueError as e:
        raise URLNotAllowedError(f"URL 主机名非法: {e}") from e

    if not host:
        raise URLNotAllowedError("URL 缺少主机名")

    if host.lower() in policy.allowlist:
        return

    if not (policy.block_metadata_endpoints or policy.block_private_networks):
        return

    for ip in _resolve_host(host):
        if policy.block_metadata_endpoints and _is_metadata_address(ip):
            raise URLNotAllowedError(f"禁止访问云元数据地址: {host} -> {ip}")
        if policy.block_private_networks and _is_private_address(ip):
            raise URLNotAllowedError(
                f"禁止访问内网地址: {host} -> {ip}（如确需访问，请在配置 [SECURITY].allowlist 中放行）"
            )


def merge_query_params(url: str, params: dict[str, Any] | None) -> str:
    if not params:
        return url
    parsed = urlparse(url)
    extra = urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
    query = f"{parsed.query}&{extra}" if parsed.query else extra
    return urlunparse(parsed._replace(query=query))


__all__ = [
    "DEFAULT_ALLOWED_SCHEMES",
    "URLPolicy",
    "merge_query_params",
    "validate_url",
]
