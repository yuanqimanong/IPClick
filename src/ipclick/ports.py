"""集中定义 gRPC 与 Web 管理端的默认端口。"""

from __future__ import annotations


DEFAULT_GRPC_PORT = 9528

DEFAULT_WEB_PORT = 9527


def port_hint(port: int) -> str:
    """在连的端口本身就可疑时返回排查提示，否则返回空字符串。"""
    if port == DEFAULT_WEB_PORT:
        return (
            f"（注意：{DEFAULT_WEB_PORT} 是 **Web 管理端** 的默认端口，"
            f"gRPC 服务端默认监听 {DEFAULT_GRPC_PORT}。若你连的是没改过的默认部署，"
            f"请把客户端端口改成 {DEFAULT_GRPC_PORT}）"
        )
    return ""


__all__ = [
    "DEFAULT_GRPC_PORT",
    "DEFAULT_WEB_PORT",
    "port_hint",
]
