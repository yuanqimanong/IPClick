"""保持人类输出与机器可解析 JSON 输出一致的 CLI 契约。"""

from __future__ import annotations

from enum import IntEnum
import json
import sys
from typing import Any, NoReturn

import click


class Exit(IntEnum):
    """稳定的进程退出码分类。"""

    OK = 0
    FAILED = 1
    USAGE = 2
    UNREACHABLE = 3
    UNAUTHENTICATED = 4
    REJECTED = 5


DEFAULT_BODY_LIMIT = 64 * 1024


def json_option(func: Any) -> Any:
    """为 Click 命令添加统一的 ``--json`` 开关。"""
    return click.option(
        "--json",
        "-J",
        "as_json",
        is_flag=True,
        default=False,
        help="输出单个 JSON 文档到 stdout（供程序 / AI 解析）",
    )(func)


def dumps(payload: Any) -> str:
    """以 UTF-8 友好的格式序列化 CLI JSON 文档。"""
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def emit(payload: dict[str, Any], *, as_json: bool, human: str = "") -> None:
    """按调用模式输出单个 JSON 文档或人类可读文本。

    成功文档同样补齐 ``ok`` 与 ``exit_code``：SKILL.md 承诺"每个文档都有 ok 和
    exit_code"，而调用方拿不到进程退出码时就靠这两个字段判断。原先只有失败路径
    （fail()）带 exit_code，成功路径缺，于是同一份契约在一半场景下不成立。
    """
    if as_json:
        click.echo(dumps({"ok": True, "exit_code": int(Exit.OK), **payload}))
    elif human:
        click.echo(human)


def note(message: str) -> None:
    """把进度或元信息写入 stderr，避免污染 stdout 数据流。"""
    click.echo(message, err=True)


def fail(
    message: str,
    code: Exit = Exit.FAILED,
    *,
    as_json: bool = False,
    **extra: Any,
) -> NoReturn:
    """输出统一失败结构并以指定分类退出。"""
    if as_json:
        click.echo(dumps({"ok": False, "error": message, "exit_code": int(code), **extra}))
    else:
        click.echo(message, err=True)
    sys.exit(int(code))


def classify(error: BaseException) -> Exit:
    """把公共异常映射为稳定退出码。"""
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
