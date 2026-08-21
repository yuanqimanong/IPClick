from __future__ import annotations

import os
from pathlib import Path

import pytest

from ipclick.config_loader.dotenv import load_dotenv, parse_env


def test_parse_env_supports_comments_after_quoted_values() -> None:
    parsed = parse_env(
        """
TOKEN="hash # stays" # comment is discarded
PATH_VALUE='C:\\Program Files\\IPClick'  # literal string
ESCAPED="line\\nquote: \\"ok\\"" # trailing comment
"""
    )

    assert parsed == {
        "TOKEN": "hash # stays",
        "PATH_VALUE": "C:\\Program Files\\IPClick",
        "ESCAPED": 'line\nquote: "ok"',
    }


def test_an_empty_real_env_var_does_not_mask_the_dotenv_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """容器编排里 `environment: - IPCLICK_AUTH_TOKEN` 这种不带值的透传会塞一个空串。

    按"已设置"处理的话，.env 里配好的令牌会被它悄悄顶掉——既不用环境变量也不用 .env，
    直接掉到默认值，鉴权就这么没了。项目对 .env 自己的约定本来就是"留空 = 不设置"。
    """
    env_file = tmp_path / ".env"
    env_file.write_text("IPCLICK_AUTH_TOKEN=from-dotenv\n", encoding="utf-8")

    monkeypatch.setenv("IPCLICK_AUTH_TOKEN", "")
    _ = load_dotenv(env_file)
    assert os.environ["IPCLICK_AUTH_TOKEN"] == "from-dotenv"

    # 非空的真实环境变量仍然优先
    monkeypatch.setenv("IPCLICK_AUTH_TOKEN", "from-real-env")
    _ = load_dotenv(env_file)
    assert os.environ["IPCLICK_AUTH_TOKEN"] == "from-real-env"
