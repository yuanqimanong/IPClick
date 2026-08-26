"""就地更新 ``.env`` 里的机密项，保留注释和其余全部内容。

和 ``writer.py`` 写 TOML 是同一套思路：只换目标那一行，不重排、不丢注释。差别在于
``.env`` 里全是机密，所以**不留 ``.bak``**——一份 644 的备份会把刚收紧的 600 白费掉。
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
import stat
import tempfile

from ipclick.exceptions import ConfigError
from ipclick.utils.log_util import log


# 需要引号的字符：空白会被 dotenv 当值的一部分留下但容易看错，'#' 会被当行尾注释，
# 引号和反斜杠则会被它的转义解析改写。落到这些情况就整段用双引号包起来。
_NEEDS_QUOTES = set(" \t\"'\\#")


def format_env_value(value: str) -> str:
    """把值编码成 dotenv 解析器能原样读回来的形式。"""
    if not value:
        return ""
    if not any(char in _NEEDS_QUOTES for char in value) and value == value.strip():
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{escaped}"'


def _key_of(line: str) -> str:
    """取出一行 dotenv 赋值语句的键名；不是赋值行时返回空串。"""
    text = line.strip()
    if not text or text.startswith("#"):
        return ""
    if text.startswith("export "):
        text = text[len("export ") :].lstrip()
    key, sep, _ = text.partition("=")
    return key.strip() if sep else ""


def set_env_values(text: str, values: dict[str, str]) -> tuple[str, list[str]]:
    """定点更新 dotenv 文本，返回新文本和实际改动的键。

    值相同就不算改动——Web 端每次保存都会把整组机密传进来，不筛一遍的话
    「已更新 0 项」会变成「已更新 6 项」，提示就没意义了。
    """
    lines = text.splitlines()
    changed: list[str] = []
    remaining = dict(values)

    for index, line in enumerate(lines):
        key = _key_of(line)
        if key not in remaining:
            continue
        new_line = f"{key}={format_env_value(remaining.pop(key))}"
        if lines[index] != new_line:
            lines[index] = new_line
            changed.append(key)

    for key, value in remaining.items():
        if not value:
            # 文件里本来没有这一项，要设的又是空值——那就是"保持不设置"，别追加一行噪音。
            continue
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"{key}={format_env_value(value)}")
        changed.append(key)

    return "\n".join(lines) + "\n", changed


def save_env(path: Path, text: str) -> None:
    """原子写入 ``.env``，新建时用 600 权限。"""
    path = Path(path)
    temp: Path | None = None
    fd: int | None = None
    try:
        # 已存在就沿用它现有的权限（用户可能自己放宽过，别悄悄改）；新建一律 600。
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temp = Path(temp_name)
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None
            _ = f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        temp = None
    except OSError as e:
        raise ConfigError(f"写入 {path} 失败：{e}") from e
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        if temp is not None:
            with contextlib.suppress(OSError):
                temp.unlink()

    log.info(f"机密已写回 {path}")


def env_target_path() -> Path:
    """选出该写哪个 ``.env``——必须是加载器回头会去读的那一个。

    加载器只在**当前工作目录**找 ``.env``（不向上递归），所以写到别处等于写进黑洞。
    """
    from ipclick.config_loader.dotenv import DEFAULT_ENV_FILENAME, find_env_file

    return find_env_file() or Path.cwd() / DEFAULT_ENV_FILENAME


def update_env_file(values: dict[str, str], path: Path | None = None) -> tuple[Path, list[str]]:
    """把机密写进 ``.env``；文件不存在时先落一份带注释的模板。"""
    target = Path(path) if path is not None else env_target_path()
    if target.exists():
        # utf-8-sig：记事本 / Set-Content 存出来的 .env 带 BOM，跟 dotenv 读取端保持一致。
        text = target.read_text(encoding="utf-8-sig")
    else:
        from ipclick.config_loader.loader import example_env

        text = example_env()
        log.info(f"{target} 不存在，将以机密模板为基础创建")

    new_text, changed = set_env_values(text, values)
    if changed or not target.exists():
        save_env(target, new_text)
    return target, changed


__all__ = [
    "env_target_path",
    "format_env_value",
    "save_env",
    "set_env_values",
    "update_env_file",
]
