from __future__ import annotations

from pathlib import Path
import tomllib

from ipclick.config_loader.writer import save, set_values


def test_inline_table_update_preserves_escaped_quotes_and_commas() -> None:
    source = '[CLUSTER]\ndiscovery = { mode = "dns", name = "a\\"b,c", refresh = 30 }\n'

    updated, changes = set_values(source, {"CLUSTER.discovery": {"refresh": 60}})

    parsed = tomllib.loads(updated)
    assert parsed["CLUSTER"]["discovery"] == {"mode": "dns", "name": 'a"b,c', "refresh": 60}
    assert changes == ["[CLUSTER].discovery.refresh = 60"]


def test_save_does_not_reuse_a_predictable_temp_file(tmp_path: Path) -> None:
    target = tmp_path / "ipclick.toml"
    stale_temp = tmp_path / "ipclick.toml.tmp"
    stale_temp.write_text("do not touch", encoding="utf-8")

    save(target, "[SERVER]\nport = 9528\n", backup=False)

    assert tomllib.loads(target.read_text(encoding="utf-8"))["SERVER"]["port"] == 9528
    assert stale_temp.read_text(encoding="utf-8") == "do not touch"
    assert not list(tmp_path.glob(".ipclick.toml.*.tmp"))
