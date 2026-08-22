"""在保留原 TOML 注释和排版的前提下定点更新配置。"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import tomllib
from typing import Any

from ipclick.exceptions import ConfigError
from ipclick.utils.log_util import log


Scalar = str | int | float | bool


# TOML 基本字符串里禁止出现的裸控制字符（除了已经有专门转义的 \n \r \t）。
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def format_value(value: Any) -> str:
    """把受支持的 Python 标量或序列编码为 TOML 字面量。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(format_value(v) for v in value) + "]"
    text = str(value)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    # 其余控制字符也必须转义。只转 \n \r \t 的话，值里带一个 \x00 / \x1b 就会产出
    # 非法 TOML，然后被 save() 报成"这是 IPClick 自己的 bug，请提 issue"——而那其实
    # 只是用户输入里有个控制字符。
    escaped = _CONTROL_CHARS.sub(lambda m: f"\\u{ord(m.group()):04X}", escaped)
    return f'"{escaped}"'


_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")

_SECTION_PATH = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*")


def _check_name(name: str, kind: str) -> None:
    """键名与节名是**原样**拼进输出的，必须在拼之前挡住。

    值有转义（关不掉自己的字符串、也换不了行），键名和节名一个都没有；而 ``save()``
    只用 ``tomllib.loads`` 兜底，注入出来的东西恰好是**合法** TOML 时它拦不住。实测
    ``{"SERVER": {'x = 1\n[SECURITY]\nauth_token': 'pwned'}}`` 会把调用方原有的
    port/host 整体搬进新造出来的 [SECURITY] 表，并且顺利写进文件。

    现在的唯一调用方（Web 配置页）用的是代码里写死的字段白名单，够不到这里；
    但 ``set_values`` 是导出的公开函数，这道校验是给下一个调用方留的。
    """
    pattern = _SECTION_PATH if kind == "节名" else _BARE_KEY
    if not name or pattern.fullmatch(name) is None:
        raise ConfigError(f"非法的 TOML {kind} {name!r}：只允许字母、数字、下划线和连字符")


def _section_bounds(lines: list[str], section: str) -> tuple[int, int] | None:
    """定位简单 TOML 表头在源文本中的行区间。"""
    header = re.compile(r"^\s*\[\s*" + re.escape(section) + r"\s*\]\s*(#.*)?$")
    any_header = re.compile(r"^\s*\[")
    start: int | None = None
    for index, line in enumerate(lines):
        if start is None:
            if header.match(line):
                start = index
            continue
        if any_header.match(line):
            return start, index
    if start is None:
        return None
    return start, len(lines)


_INLINE_TABLE_RE = re.compile(r"^(\s*[^=\s]+\s*=\s*)\{(.*)\}(\s*(?:#.*)?)$")


def _split_inline_pairs(body: str) -> list[str] | None:
    """在不拆开嵌套容器和引号字符串的情况下切分 inline table。"""
    parts: list[str] = []
    current = ""
    depth = 0
    quote = ""
    escaped = False
    for char in body:
        if quote:
            current += char
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth < 0:
                return None
        elif char == "," and depth == 0:
            parts.append(current)
            current = ""
            continue
        current += char
    if depth or quote:
        return None
    if current.strip():
        parts.append(current)
    return parts


def _set_in_inline_table(lines: list[str], parent: str, table: str, key: str, literal: str) -> bool:
    """尝试更新父表中的单行 inline table，并报告是否成功。"""
    bounds = _section_bounds(lines, parent)
    if bounds is None:
        return False
    start, end = bounds
    index = _find_key(lines, start + 1, end, table)
    if index is None:
        return False

    raw = lines[index]
    newline = "\n" if raw.endswith("\n") else ""
    match = _INLINE_TABLE_RE.match(raw.rstrip("\r\n"))
    if match is None:
        return False

    head, body, tail = match.group(1), match.group(2), match.group(3)
    pairs = _split_inline_pairs(body)
    if pairs is None:
        return False

    key_re = re.compile(r"^\s*" + re.escape(key) + r"\s*=")
    for position, pair in enumerate(pairs):
        if key_re.match(pair):
            leading = pair[: len(pair) - len(pair.lstrip())]
            pairs[position] = f"{leading}{key} = {literal}"
            break
    else:
        pairs.append(f" {key} = {literal}")

    body_out = ",".join(f" {pair.strip()}" for pair in pairs)
    lines[index] = f"{head}{{{body_out} }}{tail}{newline}"
    return True


def _find_key(lines: list[str], start: int, end: int, key: str) -> int | None:
    """在给定行区间查找顶层键。"""
    pattern = re.compile(r"^(\s*)" + re.escape(key) + r"\s*=")
    for index in range(start, end):
        if pattern.match(lines[index]):
            return index
    return None


def _split_comment(text: str) -> tuple[str, str]:
    """切开 TOML 值和行尾注释，忽略字符串内部的 ``#``。"""
    in_string = False
    quote = ""
    escaped = False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_string:
            escaped = True
            continue
        if in_string:
            if char == quote:
                in_string = False
            continue
        if char in ("'", '"'):
            in_string = True
            quote = char
            continue
        if char == "#":
            return text[:index], text[index:]
    return text, ""


def _reject_multiline(section: str, key: str, old_value: str) -> None:
    """跨行的旧值不能按"一个键一行"改掉。

    原来直接重写那一行，于是数组换行写的时候剩下的 ``"old-b",`` 和 ``]`` 留在原地
    变成孤儿，产出非法 TOML，再被 save() 报成"IPClick 自己的 bug"。
    [SECURITY].auth_token 的令牌轮换写法正是多行数组。
    """
    try:
        _ = tomllib.loads(f"probe = {old_value.strip()}")
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(
            f"改不了 [{section}].{key}：它现在的值是跨行写的（多行数组或多行 inline table），"
            f"就地替换会破坏文件结构。请手工编辑这个文件（{e}）"
        ) from e


def set_values(text: str, updates: dict[str, dict[str, Any]]) -> tuple[str, list[str]]:
    """定点更新 TOML 文本并返回新文本及可展示的变更摘要。"""
    lines = text.splitlines(keepends=True)
    changes: list[str] = []

    for section, entries in updates.items():
        _check_name(section, "节名")
        for key, value in entries.items():
            _check_name(key, "键名")
            literal = format_value(value)
            bounds = _section_bounds(lines, section)

            if bounds is None:
                parent, _, table = section.rpartition(".")
                if parent and _set_in_inline_table(lines, parent, table, key, literal):
                    changes.append(f"[{parent}].{table}.{key} = {literal}")
                    continue
                # 只有"子表确实存在、但不是可就地编辑的形式"才拒绝。父节存在而子表
                # 根本没写过时，补一个 [parent.table] 节就行——原来在这里一律抛异常，
                # 而且是在循环中途抛的，同一次提交里其它已算好的修改全部跟着丢掉。
                # Web 配置页有 21 个字段落在点分节里，手写的精简 ipclick.toml 必然踩到。
                parent_bounds = _section_bounds(lines, parent) if parent else None
                if parent_bounds is not None and _find_key(lines, parent_bounds[0] + 1, parent_bounds[1], table):
                    raise ConfigError(
                        f"改不了 [{section}].{key}：配置文件里 [{parent}] 下的 {table} "
                        f"不是可以就地编辑的形式。请手工编辑这个文件"
                    )
                if lines and not lines[-1].endswith("\n"):
                    lines[-1] += "\n"
                lines.extend(["\n", f"[{section}]\n", f"{key} = {literal}\n"])
                changes.append(f"[{section}].{key} = {literal}（新增节）")
                continue

            start, end = bounds
            index = _find_key(lines, start + 1, end, key)
            if index is None:
                insert_at = start + 1
                lines.insert(insert_at, f"{key} = {literal}\n")
                changes.append(f"[{section}].{key} = {literal}（新增）")
                continue

            line = lines[index]
            prefix, _, remainder = line.partition("=")
            old_value, comment = _split_comment(remainder)
            _reject_multiline(section, key, old_value)
            newline = "\n" if line.endswith("\n") else ""
            stripped_comment = comment.rstrip("\r\n")
            suffix = f"  {stripped_comment}" if stripped_comment else ""
            lines[index] = f"{prefix.rstrip()} = {literal}{suffix}{newline}"
            changes.append(f"[{section}].{key} = {literal}")

    return "".join(lines), changes


def format_nodes(nodes: list[dict[str, Any]]) -> str:
    """把集群节点列表编码为稳定、易读的 TOML 数组。"""
    if not nodes:
        return "nodes = []\n"
    out = ["nodes = [\n"]
    for node in nodes:
        parts = [f"id = {format_value(node['id'])}", f"address = {format_value(node['address'])}"]
        if int(node.get("weight", 100)) != 100:
            parts.append(f"weight = {int(node['weight'])}")
        for key in ("region", "zone", "token"):
            if node.get(key):
                parts.append(f"{key} = {format_value(node[key])}")
        out.append("    { " + ", ".join(parts) + " },\n")
    out.append("]\n")
    return "".join(out)


def set_nodes(text: str, nodes: list[dict[str, Any]]) -> str:
    """替换或新增 ``[CLUSTER].nodes`` 配置块。"""
    lines = text.splitlines(keepends=True)
    bounds = _section_bounds(lines, "CLUSTER")
    block = format_nodes(nodes)

    if bounds is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.extend(["\n", "[CLUSTER]\n", block])
        return "".join(lines)

    start, end = bounds
    index = _find_key(lines, start + 1, end, "nodes")
    if index is None:
        lines.insert(start + 1, block)
        return "".join(lines)

    depth = 0
    stop = index
    for cursor in range(index, end):
        depth += lines[cursor].count("[") - lines[cursor].count("]")
        stop = cursor
        if depth <= 0:
            break
    lines[index : stop + 1] = [block]
    return "".join(lines)


def save(path: Path, text: str, *, backup: bool = True) -> Path | None:
    """校验 TOML 后原子写入文件，并可在写入前保留 ``.bak``。"""
    path = Path(path)
    try:
        _ = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(
            f"拒绝写入 {path}：改完之后的内容不是合法 TOML（{e}）。"
            f"文件没有被改动。这是 IPClick 自己的 bug，请带上你改的那一项去提 issue"
        ) from e

    backup_path: Path | None = None
    temp: Path | None = None
    fd: int | None = None
    try:
        if backup and path.exists():
            backup_path = path.with_suffix(path.suffix + ".bak")
            _ = shutil.copy2(path, backup_path)

        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
        # mkstemp 使用 O_EXCL 创建同目录随机文件，避免固定 .tmp 的竞态、symlink 跟随和权限继承。
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
        raise ConfigError(f"写入配置文件 {path} 失败：{e}") from e
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        if temp is not None:
            with contextlib.suppress(OSError):
                temp.unlink()

    log.info(f"配置已写回 {path}" + (f"（备份：{backup_path.name}）" if backup_path else ""))
    return backup_path


def target_path(config_path: str | Path | None = None, port: int | None = None) -> Path:
    """选择可写配置目标，并阻止修改包内默认模板。"""
    from ipclick.config_loader.loader import DEFAULT_CONFIG_PATH, candidate_names

    if config_path:
        path = Path(config_path)
        if path.resolve() == DEFAULT_CONFIG_PATH.resolve():
            raise ConfigError("不能修改包内的默认配置模板，请先 ipclick init 生成本地 ipclick.toml")
        return path
    names = candidate_names(port)
    for name in names:
        candidate = Path(name)
        if candidate.exists():
            return candidate
    return Path(names[0])


__all__ = [
    "Scalar",
    "format_nodes",
    "format_value",
    "save",
    "set_nodes",
    "set_values",
    "target_path",
]
