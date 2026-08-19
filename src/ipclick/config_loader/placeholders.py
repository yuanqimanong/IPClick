from __future__ import annotations

from typing import Any


PORT_PLACEHOLDER = "{port}"

PORT_AWARE_KEYS: dict[str, tuple[str, ...]] = {
    "TRACE": ("sqlite_path",),
    "LOG": ("output",),
}


def substitute_port(value: Any, port: int) -> Any:
    if not isinstance(value, str) or PORT_PLACEHOLDER not in value:
        return value
    return value.replace(PORT_PLACEHOLDER, str(port))


def resolve_section(section: dict[str, Any], keys: tuple[str, ...], port: int) -> dict[str, Any]:
    resolved = dict(section)
    for key in keys:
        if key in resolved:
            resolved[key] = substitute_port(resolved[key], port)
    return resolved


def resolve_for(section_name: str, section: dict[str, Any], port: int) -> dict[str, Any]:
    return resolve_section(section, PORT_AWARE_KEYS.get(section_name, ()), port)


def has_placeholder(value: Any) -> bool:
    return isinstance(value, str) and PORT_PLACEHOLDER in value


__all__ = [
    "PORT_AWARE_KEYS",
    "PORT_PLACEHOLDER",
    "has_placeholder",
    "resolve_for",
    "resolve_section",
    "substitute_port",
]
