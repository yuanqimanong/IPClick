"""目标 URL 的安全校验。

IPClick 服务端会代替调用方去请求任意 URL。若服务端监听在 0.0.0.0 且没有鉴权，
它就成了一个内网跳板（SSRF）：外部调用方可以借它访问 127.0.0.1 上的服务，
或读取云厂商的实例元数据（169.254.169.254）拿到临时凭证。

这里提供一层与适配器无关的目标校验：

* **协议白名单**（默认开启）——只允许 http/https，挡掉 file://、gopher:// 等。
* **元数据地址拦截**（默认开启）——link-local 段没有任何正当的代理用途。
* **内网地址拦截**（默认关闭）——回环/私网/保留地址。默认关闭是为了不破坏
  "在本机跑服务端、代理本机服务" 这类现有用法；部署到公网时应在配置里打开。
"""

from dataclasses import dataclass, field
import ipaddress
import socket
from typing import Any
from urllib.parse import urlencode, urlparse, urlsplit, urlunparse

from ipclick.exceptions import URLNotAllowedError


DEFAULT_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

# 云厂商实例元数据服务。无论是否开启内网拦截都会被拒绝。
_METADATA_ADDRESSES: frozenset[str] = frozenset(
    {
        "169.254.169.254",  # AWS / GCP / Azure / 阿里云 / 腾讯云
        "100.100.100.200",  # 阿里云
        "fd00:ec2::254",  # AWS IPv6
    }
)


def _is_metadata_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return str(ip) in _METADATA_ADDRESSES


def _is_private_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """回环 / 私网 / link-local / 保留 / 组播 等非公网地址。"""
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


@dataclass(frozen=True)
class URLPolicy:
    """目标 URL 的准入策略。"""

    allowed_schemes: frozenset[str] = DEFAULT_ALLOWED_SCHEMES
    block_metadata_endpoints: bool = True
    block_private_networks: bool = False
    # 即便开启了内网拦截也放行的主机名/IP（例如内网里确实要抓的服务）
    allowlist: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_config(cls, security_config: dict[str, object] | None) -> "URLPolicy":
        """从配置文件的 ``[SECURITY]`` 节构造策略。"""
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
    """把主机名解析成 IP 列表；已经是 IP 字面量时直接返回。

    解析失败返回空列表——交给适配器去报真实的网络错误，安全校验不越俎代庖。
    """
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
    """校验目标 URL 是否允许访问。

    Args:
        url: 待校验的目标 URL。
        policy: 准入策略，默认用 :class:`URLPolicy` 的默认值。

    Raises:
        URLNotAllowedError: URL 格式非法，或命中策略里的禁止项。
    """
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
    """把 params 合并进 URL 的 query。

    浏览器导航没有单独的 params 参数，只能自己拼进 URL。注意是**合并**不是覆盖：
    直接替换 query 会把 URL 里原有的参数弄丢。
    """
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
