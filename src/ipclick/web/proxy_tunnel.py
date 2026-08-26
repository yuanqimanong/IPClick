"""解析三方隧道代理的接入串，并把凭据位置换成占位符回显。

隧道服务商给的那一行里**账号密码和地址是混在一起的**，四种常见排法都有。直接把
整串塞进 ``[PROXY].tunnel_server`` 会让密码跟着 ipclick.toml 进版本库，所以这里
先把它拆成「地址部分」和「凭据部分」：前者写 toml，后者写 ``.env``。

回显走 ``render_masked()``：只按最标准的 URL 形式拼一份，凭据位置填占位符。刻意
不保存"当初是哪种格式"——那四种格式是**输入便利**，不是需要长期记住的配置；存下来
只会多一个能和 toml 里其余字段对不上的状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import final

from ipclick.exceptions import ValidationError


AUTH_KEY_PLACEHOLDER = "{IPCLICK_PROXY_AUTH_KEY}"
AUTH_PASSWORD_PLACEHOLDER = "{IPCLICK_PROXY_AUTH_PASSWORD}"

AUTO_FORMAT = "auto"

# 键是表单值，值是显示给人看的格式串——直接用服务商文档里的写法，省得再翻译一遍。
TUNNEL_FORMATS: tuple[tuple[str, str], ...] = (
    (AUTO_FORMAT, "自动识别"),
    ("url", "scheme://username:password@host:port"),
    ("host_colon_auth", "hostname:port:username:password"),
    ("auth_at_host", "username:password@hostname:port"),
    ("host_at_auth", "hostname:port@username:password"),
)

FORMAT_KEYS: frozenset[str] = frozenset(key for key, _ in TUNNEL_FORMATS)


@final
@dataclass(frozen=True)
class ParsedTunnel:
    """从接入串里拆出来的地址部分与凭据部分。"""

    scheme: str
    host: str
    port: int
    username: str
    password: str

    @property
    def credentials_are_placeholders(self) -> bool:
        """判断凭据位置是不是回显用的占位符（即用户没打算改凭据）。"""
        return self.username == AUTH_KEY_PLACEHOLDER or self.password == AUTH_PASSWORD_PLACEHOLDER


def _split_endpoint(text: str) -> tuple[str, int]:
    """把 ``host:port`` 或 ``[v6]:port`` 拆成主机和端口。"""
    raw = text.strip()
    if not raw:
        raise ValidationError("隧道地址：找不到主机和端口")

    if raw.startswith("["):
        host, closed, tail = raw.partition("]")
        if not closed or not tail.startswith(":"):
            raise ValidationError(f"隧道地址：{raw!r} 不是合法的 [IPv6]:端口")
        host, port_text = host[1:], tail[1:]
    else:
        host, sep, port_text = raw.rpartition(":")
        if not sep:
            raise ValidationError(f"隧道地址：{raw!r} 里没有端口——需要 主机:端口")

    host = host.strip()
    if not host:
        raise ValidationError(f"隧道地址：{raw!r} 里的主机是空的")
    try:
        port = int(port_text.strip())
    except ValueError:
        raise ValidationError(f"隧道地址：端口 {port_text.strip()!r} 不是整数") from None
    if not 1 <= port <= 65535:
        raise ValidationError(f"隧道地址：端口 {port} 不在 1..65535 内")
    return host, port


def _split_credentials(text: str) -> tuple[str, str]:
    """按**第一个**冒号拆账号和密码，让密码里的冒号能原样保留。"""
    username, sep, password = text.partition(":")
    if not sep:
        return username.strip(), ""
    return username.strip(), password


def _looks_like_endpoint(text: str) -> bool:
    try:
        _ = _split_endpoint(text)
    except ValidationError:
        return False
    return True


def _parse_url(raw: str) -> ParsedTunnel:
    scheme, sep, rest = raw.partition("://")
    if not sep:
        raise ValidationError(f"按 URL 格式解析失败：{raw!r} 里没有 '://'")
    scheme = scheme.strip().lower()
    if not scheme:
        raise ValidationError(f"按 URL 格式解析失败：{raw!r} 缺协议名")
    # rpartition：密码里可能带 '@'，最后一个 '@' 之后才是地址。
    creds, at, endpoint = rest.rpartition("@")
    if not at:
        creds, endpoint = "", rest
    host, port = _split_endpoint(endpoint)
    username, password = _split_credentials(creds) if creds else ("", "")
    return ParsedTunnel(scheme, host, port, username, password)


def _parse_host_colon_auth(raw: str) -> ParsedTunnel:
    # 分 4 段但只切前 3 个冒号：密码里的冒号留在最后一段里。
    parts = raw.split(":", 3)
    if len(parts) != 4:
        raise ValidationError(f"按 hostname:port:username:password 解析失败：{raw!r} 不是四段")
    host, port = _split_endpoint(f"{parts[0]}:{parts[1]}")
    return ParsedTunnel("http", host, port, parts[2].strip(), parts[3])


def _parse_auth_at_host(raw: str) -> ParsedTunnel:
    creds, at, endpoint = raw.rpartition("@")
    if not at:
        raise ValidationError(f"按 username:password@hostname:port 解析失败：{raw!r} 里没有 '@'")
    host, port = _split_endpoint(endpoint)
    username, password = _split_credentials(creds)
    return ParsedTunnel("http", host, port, username, password)


def _parse_host_at_auth(raw: str) -> ParsedTunnel:
    # partition：地址部分不可能含 '@'，所以第一个 '@' 就是分界，密码里的 '@' 得以保留。
    endpoint, at, creds = raw.partition("@")
    if not at:
        raise ValidationError(f"按 hostname:port@username:password 解析失败：{raw!r} 里没有 '@'")
    host, port = _split_endpoint(endpoint)
    username, password = _split_credentials(creds)
    return ParsedTunnel("http", host, port, username, password)


def _parse_auto(raw: str) -> ParsedTunnel:
    """按结构猜格式；两种带 '@' 的排法真撞上了就要求手动指定。"""
    if "://" in raw:
        return _parse_url(raw)

    if "@" not in raw:
        parts = raw.split(":", 3)
        if len(parts) == 4:
            return _parse_host_colon_auth(raw)
        return ParsedTunnel("http", *_split_endpoint(raw), "", "")

    # user:pass@host:port 和 host:port@user:pass 都是 a:b@c:d，光看形状分不出来。
    # 靠"哪一边像 主机:端口"来定，两边都像时不猜——猜错会把密码当主机名写进 toml。
    left, _, right = raw.partition("@")
    left_ok, right_ok = _looks_like_endpoint(left), _looks_like_endpoint(right)
    if right_ok and not left_ok:
        return _parse_auth_at_host(raw)
    if left_ok and not right_ok:
        return _parse_host_at_auth(raw)
    if left_ok and right_ok:
        raise ValidationError(
            f"{raw!r} 两种排法都讲得通（username:password@hostname:port 和 "
            f"hostname:port@username:password），自动识别不敢猜——请在上面手动选一个格式"
        )
    raise ValidationError(f"认不出 {raw!r} 的格式——请在上面手动选一个格式，或检查是不是少了端口")


_PARSERS = {
    "url": _parse_url,
    "host_colon_auth": _parse_host_colon_auth,
    "auth_at_host": _parse_auth_at_host,
    "host_at_auth": _parse_host_at_auth,
}


def parse_tunnel(raw: str, fmt: str = AUTO_FORMAT) -> ParsedTunnel:
    """按指定格式（默认自动识别）解析隧道接入串。"""
    text = (raw or "").strip()
    if not text:
        raise ValidationError("隧道代理接入串是空的")
    if fmt not in FORMAT_KEYS:
        raise ValidationError(f"未知的代理格式 {fmt!r}")
    if fmt == AUTO_FORMAT:
        return _parse_auto(text)
    return _PARSERS[fmt](text)


def render_masked_endpoint(scheme: str, endpoint: str, *, with_credentials: bool) -> str:
    """按 URL 形式包装一个已经成形的端点，凭据位置填占位符而不是真值。"""
    endpoint = (endpoint or "").strip()
    if not endpoint:
        return ""
    prefix = f"{(scheme or 'http').strip().lower()}://"
    if not with_credentials:
        return f"{prefix}{endpoint}"
    return f"{prefix}{AUTH_KEY_PLACEHOLDER}:{AUTH_PASSWORD_PLACEHOLDER}@{endpoint}"


def render_masked(scheme: str, host: str, port: object, *, with_credentials: bool) -> str:
    """按 URL 形式拼出回显串，凭据位置填占位符而不是真值。"""
    host = (host or "").strip()
    if not host:
        return ""
    try:
        port_number = int(port)  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        return ""
    if not 1 <= port_number <= 65535:
        return ""

    endpoint = f"[{host}]:{port_number}" if ":" in host and not host.startswith("[") else f"{host}:{port_number}"
    return render_masked_endpoint(scheme, endpoint, with_credentials=with_credentials)


__all__ = [
    "AUTH_KEY_PLACEHOLDER",
    "AUTH_PASSWORD_PLACEHOLDER",
    "AUTO_FORMAT",
    "FORMAT_KEYS",
    "TUNNEL_FORMATS",
    "ParsedTunnel",
    "parse_tunnel",
    "render_masked",
    "render_masked_endpoint",
]
