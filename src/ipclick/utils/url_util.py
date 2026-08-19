"""执行 URL 准入检查并合并查询参数。"""

from dataclasses import dataclass, field
import ipaddress
import socket
from typing import Any
from urllib.parse import urlencode, urlparse, urlsplit, urlunparse

from ipclick.exceptions import URLNotAllowedError
from ipclick.utils.coerce import as_bool


DEFAULT_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

_METADATA_ADDRESSES: frozenset[str] = frozenset(
    {
        "169.254.169.254",
        "100.100.100.200",
        "fd00:ec2::254",
    }
)


def _is_metadata_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """判断地址是否为已知云厂商元数据端点。"""
    candidate = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip
    return str(candidate) in _METADATA_ADDRESSES


def _is_private_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """判断地址是否属于不应从代理访问的非公网范围。"""
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


@dataclass(frozen=True)
class URLPolicy:
    """SSRF 准入策略，包括协议、元数据、内网和主机白名单。"""

    allowed_schemes: frozenset[str] = DEFAULT_ALLOWED_SCHEMES
    block_metadata_endpoints: bool = True
    block_private_networks: bool = False
    allowlist: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_config(cls, security_config: dict[str, object] | None) -> "URLPolicy":
        """从 ``[SECURITY]`` 配置构造不可变策略。"""
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
            block_metadata_endpoints=as_bool(config.get("block_metadata_endpoints"), True),
            block_private_networks=as_bool(config.get("block_private_networks"), False),
            allowlist=allowlist,
        )


def _resolve_host(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """解析主机的全部可见地址；字面量 IP 不经过 DNS。"""
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
    """按策略验证 URL；拒绝时抛出 ``URLNotAllowedError``。"""
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
        # 白名单代表明确授权，必须先于元数据和内网地址检查。
        return

    if not (policy.block_metadata_endpoints or policy.block_private_networks):
        return

    resolved = _resolve_host(host)
    if not resolved:
        # 安全开关开启时不能因 DNS 临时失败而跳过检查，随后由适配器再次解析并访问。
        raise URLNotAllowedError(f"无法解析主机 {host!r}，为避免绕过 SSRF 准入已拒绝请求")

    # 任一 A/AAAA 记录落入禁区就拒绝，避免多地址主机绕过准入。
    for ip in resolved:
        if policy.block_metadata_endpoints and _is_metadata_address(ip):
            raise URLNotAllowedError(f"禁止访问云元数据地址: {host} -> {ip}")
        if policy.block_private_networks and _is_private_address(ip):
            raise URLNotAllowedError(
                f"禁止访问内网地址: {host} -> {ip}（如确需访问，请在配置 [SECURITY].allowlist 中放行）"
            )


def merge_query_params(url: str, params: dict[str, Any] | None) -> str:
    """在保留原查询串的情况下追加非 ``None`` 参数。"""
    if not params:
        return url
    parsed = urlparse(url)
    extra = urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
    if not extra:
        return url
    query = f"{parsed.query}&{extra}" if parsed.query else extra
    return urlunparse(parsed._replace(query=query))


__all__ = [
    "DEFAULT_ALLOWED_SCHEMES",
    "URLPolicy",
    "merge_query_params",
    "validate_url",
]
