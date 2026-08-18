"""``ipclick skill`` —— 把技能包交给 AI 代理。

命令组单独一个文件（而不是塞进 :mod:`ipclick.cli.agent`）：那边是"给 AI 用的命令"，
这边是"让 AI 知道有那些命令"。两件事在依赖上也是分开的——这里只碰
:mod:`ipclick.skill`，不需要 gRPC、配置或集群里的任何东西。
"""

from __future__ import annotations

from pathlib import Path

import click

from ipclick import __version__
from ipclick import skill as skill_pkg
from ipclick.cli.output import Exit, emit, fail, json_option


@click.group()
def skill() -> None:
    """给 AI 代理用的技能包（Claude Code Skill）。

    \b
      ipclick skill show                  # 打印到 stdout
      ipclick skill install               # 装进 ./.claude/skills/ipclick/
      ipclick skill path                  # 看包内那份在哪

    Web 管理端也提供同一份：登录后打开 /skill，或直接下载 /skill.md。
    """


@skill.command("show")
@json_option
def skill_show(as_json: bool) -> None:
    """把技能正文打到 stdout。

    重定向出来就是一个能直接用的文件：`ipclick skill show > SKILL.md`。
    所以这里不加任何前缀或提示语。
    """
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
    """把技能装进项目，让本地的 AI 代理能发现它。

    默认写 ./.claude/skills/ipclick/SKILL.md。已存在且内容被改过时不覆盖——
    加 --force 才会。
    """
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
    """包内那份技能文件在哪（只读，别直接改它——升级会被覆盖）。"""
    emit(
        {"ok": True, "path": str(skill_pkg.SKILL_FILE), "exists": skill_pkg.SKILL_FILE.exists()},
        as_json=as_json,
        human=str(skill_pkg.SKILL_FILE),
    )


__all__ = ["skill"]
