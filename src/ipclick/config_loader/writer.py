"""把配置改动写回 ``ipclick.toml``。

**为什么是文本改写而不是"读成 dict 再整体 dump"**：那份默认配置里几乎每一项
都带着解释——为什么默认是这个值、配错了会有什么症状。整体 dump 会把这些注释
全部抹掉，而它们的价值远高于代码格式的整齐。所以这里做的是定点替换：找到那一行
``key = 值``，只换等号右边，行尾注释、缩进、上下文一律保留。

**能改什么由白名单决定**（见 :mod:`ipclick.web.editable`）。这个模块只负责
"怎么改"，不负责"能不能改"——两件事分开，权限判断才不会散落在字符串处理里。

写入是原子的：先写同目录下的临时文件，再 ``os.replace``。中途断电最多丢新内容，
不会留下半个配置文件——那会让服务下次直接起不来。写之前留一份 ``.bak``。
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import tomllib
from typing import Any

from ipclick.exceptions import ConfigError
from ipclick.utils.log_util import log


Scalar = str | int | float | bool


def format_value(value: Any) -> str:
    """把 Python 值格式化成 TOML 字面量。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(format_value(v) for v in value) + "]"
    text = str(value)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{escaped}"'


def _section_bounds(lines: list[str], section: str) -> tuple[int, int] | None:
    """定位 ``[section]`` 的内容范围 ``(header_index, end_index)``。

    ``end_index`` 是下一个节头的行号（或文件末尾），即该节内容的右开边界。
    """
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
    """按**顶层**逗号切开内联表的内容。

    不能直接 ``body.split(",")``：引号里的逗号（``ua = "a,b"``）和嵌套结构里的
    逗号（``args = [1, 2]``）都会被切错，切错就等于把配置改坏。

    看不懂的结构一律返回 None，让调用方去走"明确报错"那条路——写坏一个配置文件
    的代价远大于少支持一种写法。
    """
    parts: list[str] = []
    current = ""
    depth = 0
    quote = ""
    for char in body:
        if quote:
            current += char
            if char == quote:
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
    """把 ``key = literal`` 写进 ``[parent]`` 里那个叫 ``table`` 的内联表。

    改成了返回 True，没找到可安全编辑的内联表返回 False。

    存在的理由：模板里有些子表是内联写法（``viewport = {{ width = 1920, height = 1080 }}``）
    而不是独立的 ``[BROWSER.viewport]`` 节。对这种节按"找不到节就在末尾追加一个"
    处理，会让同一个键被声明两次，产出的文件下次启动直接解析不了。
    """
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
    """在 ``[start, end)`` 里找 ``key = ...`` 的行号。

    只认没被注释掉的赋值行——模板里有大量 ``# key = ...`` 的示例，
    误把它们当成生效配置去改的话，改完还是不生效。
    """
    pattern = re.compile(r"^(\s*)" + re.escape(key) + r"\s*=")
    for index in range(start, end):
        if pattern.match(lines[index]):
            return index
    return None


def _split_comment(text: str) -> tuple[str, str]:
    """把一行赋值语句的值部分和行尾注释分开。

    要顾及字符串里的 ``#``（如 ``color = "#fff"  # 主题色``），所以要跟踪引号。
    """
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


def set_values(text: str, updates: dict[str, dict[str, Any]]) -> tuple[str, list[str]]:
    """在 TOML 文本里定点设置若干值。

    Args:
        text: 原始 TOML 全文。
        updates: ``{节名: {键: 值}}``。节名可以是 ``"A.b"`` 这种子表。

    Returns:
        ``(新文本, 变更说明列表)``。
    """
    lines = text.splitlines(keepends=True)
    changes: list[str] = []

    for section, entries in updates.items():
        for key, value in entries.items():
            literal = format_value(value)
            bounds = _section_bounds(lines, section)

            if bounds is None:
                parent, _, table = section.rpartition(".")
                if parent and _set_in_inline_table(lines, parent, table, key, literal):
                    changes.append(f"[{parent}].{table}.{key} = {literal}")
                    continue
                if parent and _section_bounds(lines, parent) is not None:
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
            _old_value, comment = _split_comment(remainder)
            newline = "\n" if line.endswith("\n") else ""
            stripped_comment = comment.rstrip("\r\n")
            suffix = f"  {stripped_comment}" if stripped_comment else ""
            lines[index] = f"{prefix.rstrip()} = {literal}{suffix}{newline}"
            changes.append(f"[{section}].{key} = {literal}")

    return "".join(lines), changes


def format_nodes(nodes: list[dict[str, Any]]) -> str:
    """把节点列表格式化成 ``nodes = [...]`` 的多行内联表数组。"""
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
    """整块替换 ``[CLUSTER].nodes``。

    这一项不能定点改单行——它是个跨多行的数组。所以找到 ``nodes = [`` 到配平的
    ``]`` 之间的区域整块换掉。数组里可能有注释掉的示例行，一起被换掉是对的：
    界面上看到的节点列表就该是文件里的节点列表。
    """
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
    """原子写入，并可留一份备份。写之前先确认它还是合法 TOML。

    **为什么要校验。** 这个模块是按行做文本编辑的，不是"读成对象再序列化回去"——
    那样做会把注释和排版全丢掉，而这个文件里的注释是给人看的主要内容。代价是
    存在产出非法 TOML 的可能：:func:`set_values` 遇到只以**内联表**形式存在的节
    （``viewport = {{ width = 1920 }}``）会在文件末尾追加一个 ``[BROWSER.viewport]``
    表头，于是同一个键被声明两次，文件下次启动直接解析不了。

    这种事故的形状特别糟：写入是成功的，界面提示"已保存"，服务照旧在跑，
    直到下一次重启才炸——那时候人早就不记得自己在网页上改过什么了。所以在这里
    拦一道：宁可保存失败并说清原因，也不要写出一个开不了机的配置。

    Returns:
        备份文件路径；没做备份则为 None。

    Raises:
        ConfigError: 内容不是合法 TOML，或写入失败（目录不可写、磁盘满等）。
    """
    path = Path(path)
    try:
        _ = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(
            f"拒绝写入 {path}：改完之后的内容不是合法 TOML（{e}）。"
            f"文件没有被改动。这是 IPClick 自己的 bug，请带上你改的那一项去提 issue"
        ) from e

    backup_path: Path | None = None
    try:
        if backup and path.exists():
            backup_path = path.with_suffix(path.suffix + ".bak")
            _ = shutil.copy2(path, backup_path)

        temp = path.with_name(path.name + ".tmp")
        mode = 0o600 if path.exists() and (path.stat().st_mode & 0o077) == 0 else 0o644
        with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode), "w", encoding="utf-8") as f:
            _ = f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    except OSError as e:
        raise ConfigError(f"写入配置文件 {path} 失败：{e}") from e

    log.info(f"配置已写回 {path}" + (f"（备份：{backup_path.name}）" if backup_path else ""))
    return backup_path


def target_path(config_path: str | Path | None = None, port: int | None = None) -> Path:
    """该往哪个文件写。

    绝不写包内的 default_config.toml——那是随包分发的模板，改它会在下次
    ``pip install --upgrade`` 时被覆盖，而且会影响同机的其他项目。

    查找顺序和 :func:`~ipclick.config_loader.loader.candidate_names` 保持一致：
    带端口时先找 ``ipclick-<端口>.toml``。两边不一致的后果是"页面上改的和进程
    实际读的不是同一个文件"，而那种错位在界面上完全看不出来。
    """
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
