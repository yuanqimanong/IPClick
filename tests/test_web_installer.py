from __future__ import annotations

import pytest

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
