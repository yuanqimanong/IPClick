from __future__ import annotations

from typing import cast, final

from grpc import ServicerContext


@final
class DetachedContext:
    def __init__(self, metadata: tuple[tuple[str, str], ...] = ()) -> None:
        self._metadata: tuple[tuple[str, str], ...] = metadata
        self.code: object = None
        self.details: str = ""

    def set_code(self, code: object) -> None:
        self.code = code

    def set_details(self, details: str) -> None:
        self.details = details

    def is_active(self) -> bool:
        return True

    def invocation_metadata(self) -> tuple[tuple[str, str], ...]:
        return self._metadata

    def as_servicer_context(self) -> ServicerContext:
        return cast(ServicerContext, cast(object, self))


__all__ = ["DetachedContext"]
