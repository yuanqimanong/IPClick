from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
import pytest

from ipclick.cli.main import main
from ipclick.secrets import SECRETS
from ipclick.web.installer import _child_env


def test_installer_child_environment_excludes_ipclick_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "test-path")
    monkeypatch.setenv("PIP_INDEX_URL", "https://packages.example/simple")
    for spec in SECRETS:
        monkeypatch.setenv(spec.env, "must-not-leak")

    child = _child_env()

    assert child["PATH"] == "test-path"
    assert child["PIP_INDEX_URL"] == "https://packages.example/simple"
    assert all(spec.env not in child for spec in SECRETS)


def test_skill_install_reports_filesystem_errors_instead_of_crashing(tmp_path: Path) -> None:
    """目标目录不可写、路径上有同名普通文件……都是很普通的文件系统错误。

    原先直接以 traceback 结束，而且 --json 下 stdout 一个字节都没有。
    """
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")

    result = CliRunner().invoke(main, ["skill", "install", "-J", "-d", str(blocker)])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert "装不了技能文件" in payload["error"]
    assert "Traceback" not in result.output
