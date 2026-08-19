"""解析允许按服务端口区分的路径配置占位符。"""

from __future__ import annotations

from typing import Any


PORT_PLACEHOLDER = "{port}"

PORT_AWARE_KEYS: dict[str, tuple[str, ...]] = {
    "TRACE": ("sqlite_path",),
    "LOG": ("output",),
}


def substitute_port(value: Any, port: int) -> Any:
    """把字符串中的 ``{port}`` 替换为实际端口，其他类型原样返回。"""
    if not isinstance(value, str) or PORT_PLACEHOLDER not in value:
        return value
    return value.replace(PORT_PLACEHOLDER, str(port))


def resolve_section(section: dict[str, Any], keys: tuple[str, ...], port: int) -> dict[str, Any]:
    """复制配置节，并只解析明确允许使用端口占位符的键。"""
    resolved = dict(section)
    for key in keys:
        if key in resolved:
            resolved[key] = substitute_port(resolved[key], port)
    return resolved


def resolve_for(section_name: str, section: dict[str, Any], port: int) -> dict[str, Any]:
    """按配置节名称应用其端口感知键清单。"""
    return resolve_section(section, PORT_AWARE_KEYS.get(section_name, ()), port)


def has_placeholder(value: Any) -> bool:
    """判断配置值是否含端口占位符。"""
    return isinstance(value, str) and PORT_PLACEHOLDER in value


__all__ = [
    "PORT_AWARE_KEYS",
    "PORT_PLACEHOLDER",
    "has_placeholder",
    "resolve_for",
    "resolve_section",
    "substitute_port",
]
