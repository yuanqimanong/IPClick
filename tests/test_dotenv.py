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


def test_a_utf8_bom_does_not_swallow_the_first_variable(tmp_path: Path) -> None:
    """带 BOM 的 .env 第一个键不能变成 "\\ufeffXXX"。

    Windows 记事本、PowerShell 的 Set-Content 存出来的 .env 开头带 U+FEFF，而
    str.strip() 不会去掉它（它不是空白字符）。于是一份直接以 IPCLICK_AUTH_TOKEN= 开头的
    .env 里，令牌那一项被解析成 "\\ufeffIPCLICK_AUTH_TOKEN"——gRPC 鉴权静默关掉，
    或者 IPCLICK_WEB_PASSWORD 被忽略、改用随机密码。随包的 init 模板第一行是注释，
    BOM 被那行吸收掉了，所以这个问题一直没露头。
    """
    env_file = tmp_path / ".env"
    env_file.write_bytes(b"\xef\xbb\xbfIPCLICK_AUTH_TOKEN=secret\nOTHER=2\n")

    parsed = parse_env(env_file.read_text(encoding="utf-8-sig"))

    assert parsed == {"IPCLICK_AUTH_TOKEN": "secret", "OTHER": "2"}


def test_load_dotenv_reads_a_bom_prefixed_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """load_dotenv 这条真实路径也要吃掉 BOM。"""
    env_file = tmp_path / ".env"
    env_file.write_bytes(b"\xef\xbb\xbfIPCLICK_AUTH_TOKEN=secret\n")
    env_file.chmod(0o600)
    monkeypatch.delenv("IPCLICK_AUTH_TOKEN", raising=False)

    applied = load_dotenv(env_file)

    assert applied == {"IPCLICK_AUTH_TOKEN": "secret"}
