"""gRPC 传输层加密与双向认证（mTLS）。

在此之前，客户端到服务端这一跳一直是明文的 ``insecure_channel``。令牌鉴权解决
的是"谁能用"，但令牌本身在不受信任的网络上会被原样嗅探到——鉴权、限流、SSRF
防护全都建在一条明文通道上。这里把这一层补上。

两个层次，分开配：

* **TLS**（``enabled``）：加密链路 + 客户端验证服务端身份。防窃听、防冒充服务端。
* **mTLS**（``require_client_cert``）：服务端反过来验证客户端证书。
  这是**通道级**身份，和令牌是两回事——令牌回答"这个调用方是谁"，
  证书回答"这条连接可不可信"。两者可以同时开，也互不替代。

配置在 ``[SECURITY.tls]``。默认全关，保持与旧部署兼容；服务端在监听非回环地址
却没开 TLS 时会打显著告警。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import grpc

from ipclick.exceptions import ConfigError
from ipclick.utils.log_util import log


def _as_path(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _read_pem(path: str, what: str) -> bytes:
    """读一个 PEM 文件。

    读不到就是配置错误，必须立刻失败——服务端带着半套 TLS 配置起来，
    比起不来危险得多。
    """
    file = Path(path).expanduser()
    try:
        data = file.read_bytes()
    except OSError as e:
        raise ConfigError(f"读取{what}失败（{file}）：{e}") from e
    if not data.strip():
        raise ConfigError(f"{what}是空文件：{file}")
    if b"-----BEGIN" not in data:
        raise ConfigError(f"{what}看起来不是 PEM 格式：{file}")
    return data


@dataclass(frozen=True)
class TLSSettings:
    """来自 ``[SECURITY.tls]``。服务端与客户端共用同一个结构，各取所需。"""

    enabled: bool = False

    cert_file: str | None = None
    key_file: str | None = None

    ca_file: str | None = None

    require_client_cert: bool = False

    server_name_override: str | None = None

    @classmethod
    def from_config(cls, security_config: dict[str, Any] | None) -> "TLSSettings":
        config = dict((security_config or {}).get("tls") or {})
        defaults = cls()
        return cls(
            enabled=bool(config.get("enabled", defaults.enabled)),
            cert_file=_as_path(config.get("cert_file")),
            key_file=_as_path(config.get("key_file")),
            ca_file=_as_path(config.get("ca_file")),
            require_client_cert=bool(config.get("require_client_cert", defaults.require_client_cert)),
            server_name_override=_as_path(config.get("server_name_override")),
        )

    @property
    def has_client_identity(self) -> bool:
        """客户端是否配了自己的证书（服务端要求 mTLS 时必需）。"""
        return bool(self.cert_file and self.key_file)


def server_credentials(settings: TLSSettings) -> grpc.ServerCredentials:
    """构造服务端 TLS 凭据。

    Raises:
        ConfigError: 证书配置不完整或文件读不到。
    """
    if not settings.cert_file or not settings.key_file:
        raise ConfigError("启用 TLS 需要同时配置 [SECURITY.tls].cert_file 与 key_file（服务端证书与私钥）")

    private_key = _read_pem(settings.key_file, "服务端私钥")
    certificate = _read_pem(settings.cert_file, "服务端证书")

    root_certificates: bytes | None = None
    if settings.ca_file:
        root_certificates = _read_pem(settings.ca_file, "客户端 CA 证书")
    elif settings.require_client_cert:
        raise ConfigError(
            "[SECURITY.tls].require_client_cert = true 时必须同时配置 ca_file，"
            "否则任何自签名客户端证书都会被接受，等于没有验证"
        )

    return grpc.ssl_server_credentials(
        [(private_key, certificate)],
        root_certificates=root_certificates,
        require_client_auth=settings.require_client_cert,
    )


def channel_credentials(settings: TLSSettings) -> grpc.ChannelCredentials:
    """构造客户端 TLS 凭据。

    Raises:
        ConfigError: 只配了证书或只配了私钥，或文件读不到。
    """
    root_certificates = _read_pem(settings.ca_file, "服务端 CA 证书") if settings.ca_file else None

    if bool(settings.cert_file) != bool(settings.key_file):
        raise ConfigError("[SECURITY.tls] 的 cert_file 与 key_file 必须成对出现（客户端证书与私钥）")

    private_key = _read_pem(settings.key_file, "客户端私钥") if settings.key_file else None
    certificate_chain = _read_pem(settings.cert_file, "客户端证书") if settings.cert_file else None

    return grpc.ssl_channel_credentials(
        root_certificates=root_certificates,
        private_key=private_key,
        certificate_chain=certificate_chain,
    )


def channel_options(settings: TLSSettings) -> list[tuple[str, Any]]:
    """TLS 相关的 channel 选项，追加到通用选项之后。"""
    if settings.enabled and settings.server_name_override:
        return [("grpc.ssl_target_name_override", settings.server_name_override)]
    return []


def warn_if_insecure(settings: TLSSettings, host: str) -> None:
    """服务端监听非回环地址却没开 TLS 时告警。

    只监听 127.0.0.1 时明文是可接受的（流量不出本机），不必吵。
    """
    if settings.enabled:
        return
    loopback = {"127.0.0.1", "::1", "localhost"}
    if host in loopback:
        return
    log.warning(
        f"服务端监听 {host} 但未启用 TLS，链路为明文——鉴权令牌会被同网段嗅探到。"
        "请配置 [SECURITY.tls]，或确保本端口只在可信网络内可达"
    )


def describe(settings: TLSSettings) -> str:
    """一句话描述当前 TLS 状态，用于启动日志与 CLI。"""
    if not settings.enabled:
        return "未启用（明文）"
    if settings.require_client_cert:
        return "已启用 TLS + 客户端证书验证（mTLS）"
    return "已启用 TLS（仅验证服务端）"


__all__ = [
    "TLSSettings",
    "channel_credentials",
    "channel_options",
    "describe",
    "server_credentials",
    "warn_if_insecure",
]
