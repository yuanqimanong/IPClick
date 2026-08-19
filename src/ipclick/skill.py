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
    path: Path
    written: bool
    unchanged: bool = False

    @property
    def message(self) -> str:
        if self.written:
            return f"已写入 {self.path}"
        if self.unchanged:
            return f"{self.path} 已是最新，未改动"
        return f"{self.path} 已存在且内容不同——要覆盖请加 --force"


def markdown(version: str | None = None) -> str:
    if version is None:
        from ipclick import __version__

        version = __version__
    return SKILL_FILE.read_text(encoding="utf-8").replace("{{VERSION}}", version)


def description() -> str:
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
