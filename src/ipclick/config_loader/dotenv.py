"""``.env`` 文件加载。

自己写而不是引 ``python-dotenv``：核心依赖刚从 24 个包精简到 17 个，为一个
四十行的解析器再加一条依赖不划算。支持的语法是 dotenv 里实际会用到的那部分：

* ``KEY=VALUE``、``export KEY=VALUE``
* ``#`` 开头的整行注释，以及**未被引号包裹**的行尾注释
* 单/双引号包裹的值；双引号内支持 ``\\n`` ``\\t`` ``\\"`` ``\\\\`` 转义
* 值两侧的空白会被去掉（引号内的保留）

**不支持**：多行值、``${VAR}`` 变量插值。需要这些请直接用环境变量。

已存在的环境变量优先
--------------------
``.env`` **不会覆盖**进程里已有的环境变量。这是 dotenv 的通行约定，也是唯一
说得通的顺序：容器编排、CI、systemd 注入的变量必须能压过仓库里那个用于本地开发
的 ``.env``，否则部署环境会被开发默认值悄悄改掉。
"""

import os
from pathlib import Path

from ipclick.utils.log_util import log


DEFAULT_ENV_FILENAME = ".env"

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}


def _unescape(text: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            nxt = text[index + 1]
            if nxt in _ESCAPES:
                out.append(_ESCAPES[nxt])
                index += 2
                continue
        out.append(char)
        index += 1
    return "".join(out)


def _strip_inline_comment(value: str) -> str:
    """去掉未被引号包裹的行尾注释。

    ``KEY=a#b`` 里的 ``#`` 是值的一部分（没有空格分隔），
    ``KEY=a  # 注释`` 里的才是注释——按 dotenv 的惯例，要求 ``#`` 前有空白。
    """
    for index, char in enumerate(value):
        if char == "#" and index > 0 and value[index - 1] in " \t":
            return value[:index]
    return value


def parse_env(text: str) -> dict[str, str]:
    """把 ``.env`` 的内容解析成键值对。解析不了的行直接跳过，不报错。"""
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()

        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            continue

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            quote = value[0]
            value = value[1:-1]
            if quote == '"':
                value = _unescape(value)
        else:
            value = _strip_inline_comment(value).strip()

        result[key] = value
    return result


def find_env_file(explicit: str | Path | None = None, start: Path | None = None) -> Path | None:
    """找到要加载的 ``.env``。

    显式给了路径就用它（不存在则返回 None）；否则只在**当前工作目录**找，
    不向上递归——向上找会让"在项目子目录里跑命令"意外加载到别处的 ``.env``，
    而配置来源不明确是最难排查的一类问题。
    """
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    base = start or Path.cwd()
    candidate = base / DEFAULT_ENV_FILENAME
    return candidate if candidate.is_file() else None


def _warn_if_world_readable(path: Path) -> None:
    """``.env`` 里是密钥，同组或其他用户可读就该提醒。

    只警告不改权限——擅自 chmod 别人的文件是更糟的行为。
    Windows 上 st_mode 的权限位没有 POSIX 语义，直接跳过。
    """
    if os.name != "posix":
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & 0o077:
        log.warning(f"{path} 权限为 {oct(mode & 0o777)}，同组或其他用户可读——里面是密钥。建议 chmod 600 {path}")


def load_dotenv(path: str | Path | None = None, *, override: bool = False) -> dict[str, str]:
    """把 ``.env`` 里的变量注入 ``os.environ``。

    Args:
        path: 指定文件；None 表示在当前工作目录找 ``.env``。
        override: 是否覆盖已存在的环境变量。默认 **False**——
            容器编排 / CI / systemd 注入的变量必须能压过仓库里的 ``.env``。

    Returns:
        实际写入 ``os.environ`` 的键值对（已存在且未 override 的不计入）。
    """
    env_file = find_env_file(path)
    if env_file is None:
        return {}

    _warn_if_world_readable(env_file)

    try:
        text = env_file.read_text(encoding="utf-8")
    except OSError:
        return {}

    applied: dict[str, str] = {}
    for key, value in parse_env(text).items():
        if not override and key in os.environ:
            continue
        os.environ[key] = value
        applied[key] = value
    return applied


__all__ = ["DEFAULT_ENV_FILENAME", "find_env_file", "load_dotenv", "parse_env"]
