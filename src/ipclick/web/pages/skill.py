"""内置 AI Skill 的预览和下载页面。"""

from __future__ import annotations

from typing import final

from ipclick.web.pages.context import PageContext
from ipclick.web.templates import render_skill


@final
class SkillPage:
    """读取随包发布的 Skill 内容并生成管理页。"""

    def __init__(self, ctx: PageContext) -> None:
        self.ctx: PageContext = ctx

    def skill_markdown(self) -> str:
        """返回随当前版本发布的完整 Skill Markdown。"""
        from ipclick import skill

        return skill.markdown()

    def skill_page(self, username: str, csrf: str) -> str:
        """渲染包含版本与安装路径提示的 Skill 页面。"""
        from ipclick import __version__, skill

        return render_skill(
            self.skill_markdown(),
            username,
            csrf,
            version=__version__,
            description=skill.description(),
            install_dir=str(skill.DEFAULT_INSTALL_DIR / skill.SKILL_NAME),
        )
