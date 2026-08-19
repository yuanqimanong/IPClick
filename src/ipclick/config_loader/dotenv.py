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
    for index, char in enumerate(value):
        if char == "#" and index > 0 and value[index - 1] in " \t":
            return value[:index]
    return value


def parse_env(text: str) -> dict[str, str]:
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
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    base = start or Path.cwd()
    candidate = base / DEFAULT_ENV_FILENAME
    return candidate if candidate.is_file() else None


def _warn_if_world_readable(path: Path) -> None:
    if os.name != "posix":
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & 0o077:
        log.warning(f"{path} 权限为 {oct(mode & 0o777)}，同组或其他用户可读——里面是密钥。建议 chmod 600 {path}")


def load_dotenv(path: str | Path | None = None, *, override: bool = False) -> dict[str, str]:
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
