from __future__ import annotations

from pathlib import Path

import click

from ipclick import __version__
from ipclick import skill as skill_pkg
from ipclick.cli.output import Exit, emit, fail, json_option


@click.group()
def skill() -> None:
    pass


@skill.command("show")
@json_option
def skill_show(as_json: bool) -> None:
    text = skill_pkg.markdown()
    if as_json:
        emit(
            {
                "ok": True,
                "name": skill_pkg.SKILL_NAME,
                "version": __version__,
                "description": skill_pkg.description(),
                "markdown": text,
            },
            as_json=True,
        )
    else:
        click.echo(text, nl=False)


@skill.command("install")
@click.option(
    "--dir",
    "-d",
    "target_dir",
    type=click.Path(path_type=Path),
    default=None,
    help=f"技能目录，默认 {skill_pkg.DEFAULT_INSTALL_DIR}（Claude Code 的项目级技能位置）",
)
@click.option("--force", "-f", is_flag=True, default=False, help="已存在且内容不同时覆盖")
@json_option
def skill_install(target_dir: Path | None, force: bool, as_json: bool) -> None:
    result = skill_pkg.install(target_dir, force=force)
    if not result.written and not result.unchanged:
        fail(result.message, Exit.FAILED, as_json=as_json, path=str(result.path))

    emit(
        {
            "ok": True,
            "path": str(result.path),
            "written": result.written,
            "unchanged": result.unchanged,
            "version": __version__,
        },
        as_json=as_json,
        human=f'{result.message}\n下一步：重开一次 AI 会话让它发现这个技能，然后直接说"用 ipclick 抓一下 …"',
    )


@skill.command("path")
@json_option
def skill_path(as_json: bool) -> None:
    emit(
        {"ok": True, "path": str(skill_pkg.SKILL_FILE), "exists": skill_pkg.SKILL_FILE.exists()},
        as_json=as_json,
        human=str(skill_pkg.SKILL_FILE),
    )


__all__ = ["skill"]
