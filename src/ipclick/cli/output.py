from __future__ import annotations

from enum import IntEnum
import json
import sys
from typing import Any, NoReturn

import click


class Exit(IntEnum):
    OK = 0
    FAILED = 1
    USAGE = 2
    UNREACHABLE = 3
    UNAUTHENTICATED = 4
    REJECTED = 5


DEFAULT_BODY_LIMIT = 64 * 1024


def json_option(func: Any) -> Any:
    return click.option(
        "--json",
        "-J",
        "as_json",
        is_flag=True,
        default=False,
        help="输出单个 JSON 文档到 stdout（供程序 / AI 解析）",
    )(func)


def dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def emit(payload: dict[str, Any], *, as_json: bool, human: str = "") -> None:
    if as_json:
        click.echo(dumps(payload))
    elif human:
        click.echo(human)


def note(message: str) -> None:
    click.echo(message, err=True)


def fail(
    message: str,
    code: Exit = Exit.FAILED,
    *,
    as_json: bool = False,
    **extra: Any,
) -> NoReturn:
    if as_json:
        click.echo(dumps({"ok": False, "error": message, "exit_code": int(code), **extra}))
    else:
        click.echo(message, err=True)
    sys.exit(int(code))


def classify(error: BaseException) -> Exit:
    from ipclick.exceptions import AuthenticationError, ConfigError, TransportError, ValidationError
    from ipclick.limiter import HostLimitTimeout

    if isinstance(error, AuthenticationError):
        return Exit.UNAUTHENTICATED
    if isinstance(error, TransportError):
        return Exit.UNREACHABLE
    if isinstance(error, (ValidationError, ConfigError)):
        return Exit.REJECTED
    if isinstance(error, HostLimitTimeout):
        return Exit.FAILED
    return Exit.FAILED


__all__ = [
    "DEFAULT_BODY_LIMIT",
    "Exit",
    "classify",
    "dumps",
    "emit",
    "fail",
    "json_option",
    "note",
]
