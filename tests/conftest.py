from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from ipclick.config_loader import loader
from ipclick.trace import reset_recorder
from ipclick.utils.config_util import Settings


_LEAKY_ENV = (
    "IPCLICK_HOST",
    "IPCLICK_PORT",
    "IPCLICK_MAX_WORKERS",
    "IPCLICK_MODE",
    "IPCLICK_LOG_LEVEL",
    "IPCLICK_CLUSTER_SELF_ID",
    "IPCLICK_AUTH_TOKEN",
    "IPCLICK_CLUSTER_SECRET",
)


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in _LEAKY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(loader, "HOME_CONFIG_PATH", tmp_path / "absent-home" / "config.toml")
    monkeypatch.chdir(tmp_path)
    loader.load_config.cache_clear()
    reset_recorder()
    yield
    loader.load_config.cache_clear()
    reset_recorder()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        {
            "SERVER": {"host": "127.0.0.1", "port": 19528, "max_workers": 2},
            "SECURITY": {},
            "DOWNLOADER": {},
            "BROWSER": {"enabled": False},
            "CLUSTER": {},
            "TRACE": {"sqlite_enabled": False},
        }
    )
