"""随包分发的 AI 技能包（Skill）。

一个 Skill 就是一份 Markdown：前置元数据说明"什么时候该用我"，正文说明"怎么用"。
Claude Code 这类代理会在需要时把它读进上下文。

**为什么它必须随包走，而不是写在 README 里。** README 是给人看的，几万字、按主题
组织、混着部署与设计取舍；模型要的是一页纸的操作手册，且必须和**这台机器上装的
这个版本**一致。跟着 wheel 分发，`ipclick skill install` 就能保证这两件事——
版本一升级，重装一次技能包，用法说明跟着变。

同一份文本有两个出口：

* :func:`markdown` —— ``ipclick skill show`` 与 Web 端 ``/skill.md`` 用它；
* :func:`install` —— ``ipclick skill install`` 把它写进项目的技能目录。

正文里的 ``{{VERSION}}`` 在读取时替换。刻意只留这一个占位符：技能文档里写死一个
具体的 host:port 只会误导——那要看用户自己的配置，而文档里已经让模型先跑
``ipclick status`` 去问了。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import final


#: 技能名。同时是安装目录名与 Claude Code 里的调用名。
SKILL_NAME = "ipclick"

#: 包内技能目录。``src/ipclick/skills/ipclick/SKILL.md``
SKILL_ROOT = Path(__file__).parent / "skills" / SKILL_NAME

#: 技能正文文件
SKILL_FILE = SKILL_ROOT / "SKILL.md"

#: 默认安装位置（相对于项目根）。Claude Code 从这里发现项目级技能。
DEFAULT_INSTALL_DIR = Path(".claude") / "skills"


@final
@dataclass(frozen=True)
class InstallResult:
    """一次安装的结果。"""

    path: Path
    written: bool
    #: 已存在且内容一致时为真——这时"没写"不是失败，是本来就对
    unchanged: bool = False

    @property
    def message(self) -> str:
        if self.written:
            return f"已写入 {self.path}"
        if self.unchanged:
            return f"{self.path} 已是最新，未改动"
        return f"{self.path} 已存在且内容不同——要覆盖请加 --force"


def markdown(version: str | None = None) -> str:
    """技能正文。``version`` 留空时取当前安装的版本号。"""
    if version is None:
        from ipclick import __version__

        version = __version__
    return SKILL_FILE.read_text(encoding="utf-8").replace("{{VERSION}}", version)


def description() -> str:
    """前置元数据里的 ``description``。

    Web 端拿它做页面副标题——那一行同时也是模型判断"要不要用这个技能"的依据，
    两处显示同一句话能省掉一次"文档和实现对不对得上"的怀疑。
    """
    text = markdown()
    if not text.startswith("---"):
        return ""
    _, _, rest = text.partition("---")
    front, _, _ = rest.partition("\n---")
    for line in front.splitlines():
        if line.startswith("description:"):
            return line.partition(":")[2].strip()
    return ""


def install(
    target_dir: Path | None = None,
    *,
    force: bool = False,
    version: str | None = None,
) -> InstallResult:
    """把技能写进 ``<target_dir>/ipclick/SKILL.md``。

    已存在且内容不同时**不覆盖**（除非 ``force``）：用户可能按自己的用法改过它，
    一次例行的版本升级不该把那些改动冲掉。内容完全一致时视为成功。
    """
    root = (target_dir or DEFAULT_INSTALL_DIR) / SKILL_NAME
    path = root / "SKILL.md"
    content = markdown(version)

    if path.exists():
        if path.read_text(encoding="utf-8") == content:
            return InstallResult(path, written=False, unchanged=True)
        if not force:
            return InstallResult(path, written=False)

    root.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(content, encoding="utf-8")
    return InstallResult(path, written=True)


__all__ = [
    "DEFAULT_INSTALL_DIR",
    "SKILL_FILE",
    "SKILL_NAME",
    "SKILL_ROOT",
    "InstallResult",
    "description",
    "install",
    "markdown",
]
