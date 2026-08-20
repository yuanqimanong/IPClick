"""集中定义 gRPC、Web 管理端的新旧默认端口。"""

from __future__ import annotations


DEFAULT_GRPC_PORT = 9528

DEFAULT_WEB_PORT = 9527

LEGACY_GRPC_PORT = 9527
LEGACY_WEB_PORT = 9530


def port_hint(port: int) -> str:
    """在命中历史默认端口时返回迁移提示，否则返回空字符串。"""
    if port == DEFAULT_WEB_PORT:
        return (
            f"（注意：0.5.0 起 {DEFAULT_WEB_PORT} 是 **Web 管理端** 的默认端口，"
            f"gRPC 服务端默认端口改成了 {DEFAULT_GRPC_PORT}。若你连的是自己没改过的默认部署，"
            f"请把客户端端口改成 {DEFAULT_GRPC_PORT}）"
        )
    if port == LEGACY_WEB_PORT:
        return f"（注意：0.5.0 起 Web 管理端默认端口从 {LEGACY_WEB_PORT} 改成了 {DEFAULT_WEB_PORT}）"
    return ""


__all__ = [
    "DEFAULT_GRPC_PORT",
    "DEFAULT_WEB_PORT",
    "LEGACY_GRPC_PORT",
    "LEGACY_WEB_PORT",
    "port_hint",
]
