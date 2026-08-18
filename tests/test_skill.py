"""随包分发的 AI 技能包。

守两件事：**它确实在包里**（打包漏掉非 .py 文件是最容易发生也最难当场发现的），
以及**安装不会冲掉用户改过的那份**。
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
import pytest

from ipclick import __version__, skill
from ipclick.cli.main import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestPackaging:
    def test_file_ships_with_the_package(self):
        """漏进 wheel 的话，`ipclick skill install` 在装好的环境里会直接崩。"""
        assert skill.SKILL_FILE.exists(), f"{skill.SKILL_FILE} 不在包里"

    def test_has_valid_frontmatter(self):
        text = skill.markdown()
        assert text.startswith("---\n")
        head = text.split("---", 2)[1]
        assert "name: ipclick" in head
        assert "description:" in head

    def test_description_is_extracted(self):
        """这一行同时是模型判断"要不要用"的依据，和 Web 端的页面副标题。"""
        assert "IPClick" in skill.description()

    def test_version_placeholder_is_substituted(self):
        text = skill.markdown()
        assert "{{VERSION}}" not in text
        assert __version__ in text

    def test_documents_the_output_contract(self):
        """技能里要是没写清 --json 与退出码，模型只能靠猜。"""
        text = skill.markdown()
        assert "--json" in text
        assert "exit_code" in text
        for code in ("0", "1", "3", "4", "5"):
            assert f"| {code} |" in text

    def test_mentions_body_truncation(self):
        """最容易踩的坑：把截断的 HTML 当完整页面。"""
        assert "body_truncated" in skill.markdown()


class TestInstall:
    def test_writes_to_the_default_location(self, tmp_path: Path):
        result = skill.install(tmp_path)
        assert result.written is True
        assert result.path == tmp_path / "ipclick" / "SKILL.md"
        assert result.path.read_text(encoding="utf-8") == skill.markdown()

    def test_reinstall_of_identical_content_is_not_a_failure(self, tmp_path: Path):
        _ = skill.install(tmp_path)
        again = skill.install(tmp_path)
        assert again.written is False
        assert again.unchanged is True

    def test_does_not_clobber_local_edits(self, tmp_path: Path):
        """用户可能按自己的用法改过它，一次例行升级不该把改动冲掉。"""
        target = tmp_path / "ipclick" / "SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text("我自己改过的", encoding="utf-8")

        result = skill.install(tmp_path)
        assert result.written is False
        assert result.unchanged is False
        assert target.read_text(encoding="utf-8") == "我自己改过的"

    def test_force_overwrites(self, tmp_path: Path):
        target = tmp_path / "ipclick" / "SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text("旧的", encoding="utf-8")

        assert skill.install(tmp_path, force=True).written is True
        assert target.read_text(encoding="utf-8") == skill.markdown()


class TestCLI:
    def test_show_is_redirectable(self, runner: CliRunner):
        """`ipclick skill show > SKILL.md` 出来的必须是一个能直接用的文件。"""
        result = runner.invoke(main, ["skill", "show"])
        assert result.exit_code == 0
        assert result.output.startswith("---\nname: ipclick")

    def test_show_json_carries_the_markdown(self, runner: CliRunner):
        result = runner.invoke(main, ["skill", "show", "--json"])
        data = json.loads(result.stdout)
        assert data["ok"] is True
        assert data["version"] == __version__
        assert data["markdown"].startswith("---")

    def test_install_reports_the_path(self, runner: CliRunner, tmp_path: Path):
        result = runner.invoke(main, ["skill", "install", "-d", str(tmp_path), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["written"] is True
        assert Path(data["path"]).exists()

    def test_install_refuses_to_clobber_and_exits_nonzero(self, runner: CliRunner, tmp_path: Path):
        target = tmp_path / "ipclick" / "SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text("我自己改过的", encoding="utf-8")

        result = runner.invoke(main, ["skill", "install", "-d", str(tmp_path), "--json"])
        assert result.exit_code != 0
        assert json.loads(result.stdout)["ok"] is False
        assert target.read_text(encoding="utf-8") == "我自己改过的"

    def test_path_points_at_the_packaged_copy(self, runner: CliRunner):
        result = runner.invoke(main, ["skill", "path", "--json"])
        data = json.loads(result.stdout)
        assert data["exists"] is True
        assert data["path"].endswith("SKILL.md")
