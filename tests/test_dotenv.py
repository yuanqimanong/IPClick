from __future__ import annotations

from ipclick.config_loader.dotenv import parse_env


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
