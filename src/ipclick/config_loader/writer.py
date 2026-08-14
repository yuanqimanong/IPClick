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
from typing import Any

from ipclick.exceptions import ConfigError
from ipclick.utils.log_util import log


#: 允许的标量类型。别的类型（dict、嵌套 list）不走这条路——
#: 节点列表那种结构由 :func:`write_nodes` 专门处理。
Scalar = str | int | float | bool


def format_value(value: Any) -> str:
    """把 Python 值格式化成 TOML 字面量。"""
    if isinstance(value, bool):
        # 必须在 int 之前判断：Python 里 bool 是 int 的子类
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(format_value(v) for v in value) + "]"
    text = str(value)
    # 双引号字符串：只需要转义反斜杠与双引号（TOML 基本字符串规则）
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    # 控制字符在 TOML 基本字符串里非法，用转义序列表示
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
                # 整节都不存在：追加到末尾。注释说明它是被界面加进来的，
                # 否则以后看到一个没有任何说明的裸节会一头雾水。
                if lines and not lines[-1].endswith("\n"):
                    lines[-1] += "\n"
                lines.extend(["\n", f"[{section}]\n", f"{key} = {literal}\n"])
                changes.append(f"[{section}].{key} = {literal}（新增节）")
                continue

            start, end = bounds
            index = _find_key(lines, start + 1, end, key)
            if index is None:
                # 节存在但没有这个键：插在节头之后，而不是节尾——
                # 节尾往前常常是一段说明下一节的注释，插在那里会被误读成属于下一节。
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

    # 找配平的右括号：数组里的内联表自己也带括号，所以要计数而不是找第一个 ]
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
    """原子写入，并可留一份备份。

    Returns:
        备份文件路径；没做备份则为 None。

    Raises:
        ConfigError: 写入失败（目录不可写、磁盘满等）。
    """
    path = Path(path)
    backup_path: Path | None = None
    try:
        if backup and path.exists():
            backup_path = path.with_suffix(path.suffix + ".bak")
            _ = shutil.copy2(path, backup_path)

        # 同目录下的临时文件 + os.replace：同一文件系统内 replace 是原子的，
        # 所以任何时刻这个路径上要么是旧内容、要么是新内容，不会是半截。
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


def target_path(config_path: str | Path | None = None) -> Path:
    """该往哪个文件写。

    绝不写包内的 default_config.toml——那是随包分发的模板，改它会在下次
    ``pip install --upgrade`` 时被覆盖，而且会影响同机的其他项目。
    """
    from ipclick.config_loader.loader import DEFAULT_CONFIG_PATH

    if config_path:
        path = Path(config_path)
        if path.resolve() == DEFAULT_CONFIG_PATH.resolve():
            raise ConfigError("不能修改包内的默认配置模板，请先 ipclick init 生成本地 ipclick.toml")
        return path
    for name in ("ipclick.toml", ".ipclick.toml"):
        candidate = Path(name)
        if candidate.exists():
            return candidate
    return Path("ipclick.toml")


__all__ = [
    "Scalar",
    "format_nodes",
    "format_value",
    "save",
    "set_nodes",
    "set_values",
    "target_path",
]
