"""供内部调用复用 TaskService 的最小 ServicerContext 实现。"""

from __future__ import annotations

from typing import cast, final

from grpc import ServicerContext


@final
class DetachedContext:
    """不依赖真实 RPC 生命周期的轻量上下文。"""

    def __init__(self, metadata: tuple[tuple[str, str], ...] = ()) -> None:
        self._metadata: tuple[tuple[str, str], ...] = metadata
        self.code: object = None
        self.details: str = ""

    def set_code(self, code: object) -> None:
        """记录内部子调用设置的 gRPC 状态码。"""
        self.code = code

    def set_details(self, details: str) -> None:
        """记录内部子调用设置的状态详情。"""
        self.details = details

    def is_active(self) -> bool:
        """内部子调用始终视为仍在等待。"""
        return True

    def invocation_metadata(self) -> tuple[tuple[str, str], ...]:
        """返回转发标记等内部 metadata。"""
        return self._metadata

    def as_servicer_context(self) -> ServicerContext:
        """仅在服务内部将最小实现收窄为 gRPC 上下文类型。"""
        return cast(ServicerContext, cast(object, self))


__all__ = ["DetachedContext"]
