"""CLI 的输出契约与退出码。

这一层存在的理由只有一个：**让另一个程序（多半是 AI）能可靠地解析结果**。
人看的输出可以随版本调整措辞，机器看的不行，所以两者分开，并且给机器的那一份
有几条不能破的规矩：

1. **``--json`` 时 stdout 上有且只有一个 JSON 文档。** 日志、警告、进度全部走
   stderr。管道那头 ``json.loads(stdout)`` 必须永远成立——这是整个契约的地基。
2. **失败也返回 JSON。** 错误信息进那个文档的 ``error`` 字段，而不是甩到
   stderr 让调用方去猜。一个只在成功时输出结构化数据的接口，等于逼调用方写两套
   解析逻辑，其中处理失败的那套永远缺乏测试。
3. **每个文档都有 ``ok`` 和 ``exit_code``。** 这样调用方即使拿不到进程退出码
   （很多代理框架只把 stdout 递回来）也能判断成败，而且判断依据和 shell 里看到的
   那个数字**是同一个**。
4. **退出码分类而不是只有 0/1。** "连不上服务端"和"目标网站返回 404"都算失败，
   但前者要去看进程、后者要去看 URL。见 :class:`Exit`。
5. **响应体默认截断。** 见 :data:`DEFAULT_BODY_LIMIT`。

刻意**没有**做的：不提供 ``--quiet``、不提供自定义格式串。多一种输出形态就多一
份要维护的契约，而 ``--json`` + ``jq`` 已经覆盖了这些需求。
"""

from __future__ import annotations

from enum import IntEnum
import json
import sys
from typing import Any, NoReturn

import click


class Exit(IntEnum):
    """退出码。

    分成五类，依据是"看到这个码之后该往哪儿查"——这也是给 AI 的第一条线索。
    ``USAGE`` 的值必须是 2：那是 click 自己在参数错误时用的码，改不了也不该改。
    """

    OK = 0
    #: 请求发出去了但结果不理想：HTTP >= 400、探测不通、装包退出码非 0。
    #: 该查的是目标本身。
    FAILED = 1
    #: 命令行参数写错了（由 click 抛出，这里只是登记下来）。
    USAGE = 2
    #: 连不上 IPClick 服务端。该查的是进程起没起、地址端口对不对、防火墙。
    UNREACHABLE = 3
    #: 鉴权失败。该查的是令牌。
    UNAUTHENTICATED = 4
    #: 参数被服务端拒绝，或本地配置不合法。改调用参数或配置文件。
    REJECTED = 5


#: ``--json`` 输出里响应体的默认上限（字符）。
#:
#: 64 KiB 是给**上下文窗口**留的余量，不是给磁盘的。一个 AI 调用方拿到 5 MB 的
#: HTML 之后，那 5 MB 会原样进它的上下文——一次调用就能把整个会话挤爆，而它想要
#: 的通常只是页面开头那点东西。要完整内容请用 ``-o 文件``（不截断），或者干脆不
#: 加 ``--json``：那时响应体直接进 stdout，也不截断。
DEFAULT_BODY_LIMIT = 64 * 1024


def json_option(func: Any) -> Any:
    """给命令加上 ``--json / -J``。

    刻意做成每个命令自己的选项、而不是挂在顶层 group 上：挂在 group 上就只能写
    ``ipclick --json fetch ...``，而人和 AI 都更习惯把标志写在后面。放在命令上
    两种写法里只有一种合法，但那一种会出现在 ``--help`` 里。
    """
    return click.option(
        "--json",
        "-J",
        "as_json",
        is_flag=True,
        default=False,
        help="输出单个 JSON 文档到 stdout（供程序 / AI 解析）",
    )(func)


def dumps(payload: Any) -> str:
    """统一的序列化。``ensure_ascii=False`` 让中文原样输出，别人 grep 得到。"""
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def emit(payload: dict[str, Any], *, as_json: bool, human: str = "") -> None:
    """输出一次结果。``--json`` 时是那个 JSON 文档，否则是给人看的文本。"""
    if as_json:
        click.echo(dumps(payload))
    elif human:
        click.echo(human)


def note(message: str) -> None:
    """一句提示。**永远走 stderr**——stdout 是留给结果的。"""
    click.echo(message, err=True)


def fail(
    message: str,
    code: Exit = Exit.FAILED,
    *,
    as_json: bool = False,
    **extra: Any,
) -> NoReturn:
    """报告失败并按 :class:`Exit` 退出。

    ``--json`` 时错误也是一个正常的 JSON 文档（``ok: false``），不是 stderr 上的
    一行字。调用方于是只需要一条解析路径。
    """
    if as_json:
        click.echo(dumps({"ok": False, "error": message, "exit_code": int(code), **extra}))
    else:
        click.echo(message, err=True)
    sys.exit(int(code))


def classify(error: BaseException) -> Exit:
    """把本项目的异常翻译成退出码。

    分类的依据是 :class:`Exit` 里写的"该往哪儿查"。落不进任何一类的算
    :attr:`Exit.FAILED`——宁可让调用方多看一眼错误信息，也不要凭猜给出一个会把
    人引到错误方向的码。
    """
    from ipclick.exceptions import AuthenticationError, ConfigError, TransportError, ValidationError
    from ipclick.limiter import HostLimitTimeout

    if isinstance(error, AuthenticationError):
        return Exit.UNAUTHENTICATED
    if isinstance(error, TransportError):
        return Exit.UNREACHABLE
    if isinstance(error, (ValidationError, ConfigError)):
        return Exit.REJECTED
    # 服务端限流：请求本身没问题，是发太快了。归到 FAILED，调用方该做的是退避重试。
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
