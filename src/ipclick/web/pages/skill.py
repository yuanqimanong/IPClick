from __future__ import annotations

from typing import final

from ipclick.web.pages.context import PageContext
from ipclick.web.templates import render_skill


@final
class SkillPage:
    def __init__(self, ctx: PageContext) -> None:
        self.ctx: PageContext = ctx

    def skill_markdown(self) -> str:
        from ipclick import skill

        return skill.markdown()

    def skill_page(self, username: str, csrf: str) -> str:
        from ipclick import __version__, skill

        return render_skill(
            self.skill_markdown(),
            username,
            csrf,
            version=__version__,
            description=skill.description(),
            install_dir=str(skill.DEFAULT_INSTALL_DIR / skill.SKILL_NAME),
        )
