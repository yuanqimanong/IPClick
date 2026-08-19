"""默认端口，唯一的事实来源。

**为什么单独一个模块。** 0.4 之前 ``9527`` 这个数字硬编码在六处：服务端、SDK、
三条 CLI 命令的兜底值、Web 配置。0.5.0 要改默认端口时才发现这件事——改漏一处的
症状是"某条路径仍然连旧端口"，而它只在那一条路径被走到时才暴露。同一个常量散在
多处的代价一向如此，所以这里把它收成一个没有任何依赖的小模块（谁都能 import，
不会造成循环）。

**0.5.0 的破坏性变更**：两个端口互换了角色。

===========  =====  =====  ====================================================
用途          0.4    0.5    为什么
===========  =====  =====  ====================================================
Web 管理端    9530   9527   集群里"主控"那台机器上，人真正要打开的是它，
                            最好记的号该给它
gRPC 服务端   9527   9528   让位
===========  =====  =====  ====================================================

升级时最可能踩的坑是**客户端仍然连 9527，于是连到了 Web 端口上**——那是个 HTTP
服务，gRPC 握手会失败并给出一个和端口毫无关系的错误。:func:`port_hint` 就是为这
一种情况准备的，:meth:`ipclick.sdk.ClientBase._rpc_error` 会把它附在错误后面。
"""

from __future__ import annotations


DEFAULT_GRPC_PORT = 9528

DEFAULT_WEB_PORT = 9527

LEGACY_GRPC_PORT = 9527
LEGACY_WEB_PORT = 9530


def port_hint(port: int) -> str:
    """连不上时，看看是不是踩了 0.5.0 的端口互换。返回一句提示或空串。

    只在端口**正好**是那两个历史值时才说话——无差别地给每个连接错误都附一段
    版本变更史，只会把真正的错误挤到看不见的地方。
    """
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
