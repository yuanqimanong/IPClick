"""gRPC TLS/mTLS 配置解析、PEM 读取与凭据构造。"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import grpc

from ipclick.exceptions import ConfigError
from ipclick.utils.coerce import as_bool, as_optional_text
from ipclick.utils.log_util import log


def _read_pem(path: str, what: str) -> bytes:
    """读取非空 PEM 文件，并将文件错误转换为配置错误。"""
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
    """服务端与客户端共享的 TLS 文件及校验设置。"""

    enabled: bool = False

    cert_file: str | None = None
    key_file: str | None = None

    ca_file: str | None = None

    require_client_cert: bool = False

    server_name_override: str | None = None

    @classmethod
    def from_config(cls, security_config: dict[str, Any] | None) -> "TLSSettings":
        """从 ``[SECURITY.tls]`` 构造 TLS 设置。"""
        config = dict((security_config or {}).get("tls") or {})
        defaults = cls()
        return cls(
            enabled=as_bool(config.get("enabled"), defaults.enabled),
            cert_file=as_optional_text(config.get("cert_file")),
            key_file=as_optional_text(config.get("key_file")),
            ca_file=as_optional_text(config.get("ca_file")),
            require_client_cert=as_bool(config.get("require_client_cert"), defaults.require_client_cert),
            server_name_override=as_optional_text(config.get("server_name_override")),
        )

    @property
    def has_client_identity(self) -> bool:
        """返回客户端证书和私钥是否成对存在。"""
        return bool(self.cert_file and self.key_file)


def server_credentials(settings: TLSSettings) -> grpc.ServerCredentials:
    """构造服务端凭据，并在 mTLS 下强制要求客户端 CA。"""
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
    """构造客户端 TLS 凭据，可选携带客户端证书完成 mTLS。"""
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
    """返回仅在 TLS 开启时生效的服务端名称覆盖选项。"""
    if settings.enabled and settings.server_name_override:
        return [("grpc.ssl_target_name_override", settings.server_name_override)]
    return []


def warn_if_insecure(settings: TLSSettings, host: str) -> None:
    """非回环地址明文监听时提示令牌可能被窃听。"""
    if settings.enabled:
        return
    loopback = {"127.0.0.1", "::1", "localhost"}
    if host.strip("[]") in loopback:
        return
    log.warning(
        f"服务端监听 {host} 但未启用 TLS，链路为明文——鉴权令牌会被同网段嗅探到。"
        "请配置 [SECURITY.tls]，或确保本端口只在可信网络内可达"
    )


def describe(settings: TLSSettings) -> str:
    """生成人类可读的传输安全模式描述。"""
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
