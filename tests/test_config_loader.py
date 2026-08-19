from __future__ import annotations

from pathlib import Path

import pytest

from ipclick.config_loader import loader
from ipclick.config_loader.loader import candidate_names, example_config, load_config
from ipclick.exceptions import ConfigError


def test_candidate_names_prefers_the_port_specific_file() -> None:
    assert candidate_names(9601) == ["ipclick-9601.toml", ".ipclick-9601.toml", "ipclick.toml", ".ipclick.toml"]
    assert candidate_names() == ["ipclick.toml", ".ipclick.toml"]


def test_defaults_are_loaded_when_nothing_else_exists() -> None:
    config = load_config()
    assert config["SERVER"]["port"] == 9528
    assert config["SECURITY"]["block_metadata_endpoints"] is True


def test_user_file_overrides_the_defaults(tmp_path: Path) -> None:
    (tmp_path / "ipclick.toml").write_text("[SERVER]\nport = 19999\n", encoding="utf-8")
    config = load_config()

    assert config["SERVER"]["port"] == 19999
    assert config["SERVER"]["max_workers"] == 100


def test_port_specific_file_wins_over_the_plain_one(tmp_path: Path) -> None:
    (tmp_path / "ipclick.toml").write_text("[SERVER]\nport = 1\n", encoding="utf-8")
    (tmp_path / "ipclick-9601.toml").write_text("[SERVER]\nport = 9601\n", encoding="utf-8")

    assert load_config(port=9601)["SERVER"]["port"] == 9601


def test_explicit_path_wins_over_discovery(tmp_path: Path) -> None:
    (tmp_path / "ipclick.toml").write_text("[SERVER]\nport = 1\n", encoding="utf-8")
    explicit = tmp_path / "custom.toml"
    explicit.write_text("[SERVER]\nport = 2\n", encoding="utf-8")

    assert load_config(str(explicit))["SERVER"]["port"] == 2


def test_environment_overrides_are_cast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IPCLICK_PORT", "12345")
    monkeypatch.setenv("IPCLICK_MODE", "cluster")
    loader.load_config.cache_clear()
    config = load_config()

    assert config["SERVER"]["port"] == 12345
    assert config["GENERAL"]["mode"] == "cluster"


def test_unparsable_environment_override_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IPCLICK_PORT", "not-a-port")
    loader.load_config.cache_clear()

    assert load_config()["SERVER"]["port"] == 9528


def test_empty_environment_override_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IPCLICK_PORT", "")
    loader.load_config.cache_clear()

    assert load_config()["SERVER"]["port"] == 9528


def test_broken_toml_is_rejected_instead_of_silently_using_defaults(tmp_path: Path) -> None:
    (tmp_path / "ipclick.toml").write_text("this is not = = toml", encoding="utf-8")

    with pytest.raises(ConfigError, match="不是合法 TOML"):
        load_config()


def test_missing_explicit_config_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="不存在"):
        load_config(tmp_path / "missing.toml")


def test_example_config_is_the_shipped_default() -> None:
    text = example_config()
    assert "[SERVER]" in text
    assert "[DOWNLOADER]" in text
