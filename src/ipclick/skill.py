"""读取并安装随发行包提供的 IPClick AI 技能说明。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import final


SKILL_NAME = "ipclick"

SKILL_ROOT = Path(__file__).parent / "skills" / SKILL_NAME

SKILL_FILE = SKILL_ROOT / "SKILL.md"

DEFAULT_INSTALL_DIR = Path(".claude") / "skills"


@final
@dataclass(frozen=True)
class InstallResult:
    """技能文件安装结果及其目标路径。"""

    path: Path
    written: bool
    unchanged: bool = False

    @property
    def message(self) -> str:
        """返回面向命令行用户的结果文案。"""
        if self.written:
            return f"已写入 {self.path}"
        if self.unchanged:
            return f"{self.path} 已是最新，未改动"
        return f"{self.path} 已存在且内容不同——要覆盖请加 --force"


def markdown(version: str | None = None) -> str:
    """读取技能 Markdown，并替换当前发行版本占位符。"""
    if version is None:
        from ipclick import __version__

        version = __version__
    return SKILL_FILE.read_text(encoding="utf-8").replace("{{VERSION}}", version)


def description() -> str:
    """从技能文件的 YAML front matter 中提取描述。"""
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
    """将技能文件安装到目标目录，除非 ``force`` 否则不覆盖用户修改。"""
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
