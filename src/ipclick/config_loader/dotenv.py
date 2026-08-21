"""读取项目级 ``.env``，且不覆盖进程中已显式设置的环境变量。"""

import os
from pathlib import Path

from ipclick.utils.log_util import log


DEFAULT_ENV_FILENAME = ".env"

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}


def _unescape(text: str) -> str:
    """解析双引号值中受支持的反斜杠转义。"""
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
    """移除非引号值中由空白分隔的行尾注释。"""
    for index, char in enumerate(value):
        if char == "#" and index > 0 and value[index - 1] in " \t":
            return value[:index]
    return value


def _parse_value(value: str) -> str:
    """解析单/双引号值及其后的可选注释。"""
    value = value.strip()
    if not value or value[0] not in ("'", '"'):
        return _strip_inline_comment(value).strip()

    quote = value[0]
    escaped = False
    for index in range(1, len(value)):
        char = value[index]
        if quote == '"' and escaped:
            escaped = False
            continue
        if quote == '"' and char == "\\":
            escaped = True
            continue
        if char != quote:
            continue

        tail = value[index + 1 :].strip()
        if tail and not tail.startswith("#"):
            break
        inner = value[1:index]
        return _unescape(inner) if quote == '"' else inner

    # 未闭合或引号后仍有正文时保留旧的宽松行为，避免静默吞掉用户内容。
    return _strip_inline_comment(value).strip()


def parse_env(text: str) -> dict[str, str]:
    """解析常用 dotenv 语法，忽略空行、注释和无效行。"""
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

        result[key] = _parse_value(value)
    return result


def find_env_file(explicit: str | Path | None = None, start: Path | None = None) -> Path | None:
    """查找显式文件或指定目录下的默认 ``.env``。"""
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    base = start or Path.cwd()
    candidate = base / DEFAULT_ENV_FILENAME
    return candidate if candidate.is_file() else None


def _warn_if_world_readable(path: Path) -> None:
    """在 POSIX 上提示权限过宽、可能泄露机密的环境文件。"""
    if os.name != "posix":
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & 0o077:
        log.warning(f"{path} 权限为 {oct(mode & 0o777)}，同组或其他用户可读——里面是密钥。建议 chmod 600 {path}")


def load_dotenv(path: str | Path | None = None, *, override: bool = False) -> dict[str, str]:
    """把 dotenv 值写入 ``os.environ``，并返回本次实际应用的项目。"""
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
        # 只有**非空**的真实环境变量才压过 .env。容器编排里 `environment: - IPCLICK_AUTH_TOKEN`
        # 这种不带值的透传会往进程里塞一个空串，按"已设置"处理的话，.env 里配好的令牌会被
        # 它悄悄顶掉——既不用环境变量也不用 .env，直接掉到默认值，鉴权就这么没了。
        # 项目对 .env 自己的约定本来就是"留空 = 不设置"，这里保持一致。
        if not override and os.environ.get(key, "").strip():
            continue
        os.environ[key] = value
        applied[key] = value
    return applied


__all__ = ["DEFAULT_ENV_FILENAME", "find_env_file", "load_dotenv", "parse_env"]
