from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ipclick.adapters import registry
from ipclick.services.task_service import TaskService
from ipclick.trace import TraceRecorder
from ipclick.utils.config_util import Settings
from ipclick.web.pages import WebPages

from .helpers import StubAdapter


CSRF = "csrf-token"

USER = "admin"

BASE_CONFIG = """[SERVER]
host = "127.0.0.1"
port = 19528
max_workers = 8

[SECURITY]

[DOWNLOADER]

[BROWSER]
enabled = false

[CLUSTER]

[TRACE]
sqlite_enabled = false
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "ipclick.toml"
    path.write_text(BASE_CONFIG, encoding="utf-8")
    return path


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> Iterator[TaskService]:
    monkeypatch.setitem(registry.ADAPTER_CLASSES, StubAdapter.adapter_name, StubAdapter)
    built = TaskService(
        Settings(
            {
                "SERVER": {"max_workers": 2},
                "SECURITY": {},
                "DOWNLOADER": {},
                "BROWSER": {"enabled": False},
                "CLUSTER": {},
            }
        )
    )
    yield built
    built.cleanup()


@pytest.fixture
def pages(config_file: Path, service: TaskService) -> WebPages:
    config = Settings(
        {
            "SERVER": {"host": "127.0.0.1", "port": 19528, "max_workers": 8},
            "SECURITY": {},
            "DOWNLOADER": {},
            "BROWSER": {"enabled": False},
            "CLUSTER": {},
            "TRACE": {"sqlite_enabled": False},
        }
    )
    return WebPages(
        config,
        TraceRecorder(),
        task_service=service,
        config_path=config_file,
        runtime_ports={"SERVER.port": 19528, "WEB.port": 19527},
    )


def test_trace_page_and_json_agree(pages: WebPages) -> None:
    html = pages.trace_page({}, USER, CSRF)
    payload = pages.trace_json({})

    assert "<!doctype html>" in html
    assert set(payload) >= {"records", "source"}


def test_trace_fragment_is_not_a_full_page(pages: WebPages) -> None:
    assert "<!doctype html>" not in pages.trace_fragment({})


def test_test_page_renders_without_a_result(pages: WebPages) -> None:
    assert "<!doctype html>" in pages.test_page({}, None, USER, CSRF)


def test_running_a_request_goes_through_the_local_service(pages: WebPages) -> None:
    result = pages.run_test({"url": "http://127.0.0.1/probe", "method": "GET", "adapter": "curl_cffi"})

    assert result["status_code"] == 200
    assert result["body"] == "body"
    assert result["trace"]["adapter"] == "curl_cffi"


def test_a_blocked_url_is_reported_as_an_error(pages: WebPages) -> None:
    result = pages.run_test({"url": "ftp://example.com", "method": "GET"})
    assert result.get("error")


def test_results_round_trip_through_the_stash(pages: WebPages) -> None:
    form = {"url": "http://127.0.0.1/probe", "method": "GET"}
    result = pages.run_test(form)
    token = pages.stash_test_result(form, result)

    stored_form, stored_result = pages.take_test_result(token)
    assert stored_form == form
    assert stored_result is not None
    assert stored_result["status_code"] == 200


def test_an_unknown_stash_token_yields_nothing(pages: WebPages) -> None:
    form, result = pages.take_test_result("nope")
    assert form == {}
    assert result is None


def test_curl_import_fills_the_form(pages: WebPages) -> None:
    form, notes, error = pages.import_curl({"curl": "curl -X POST http://127.0.0.1/x -H 'X-A: 1' -d 'body'"})

    assert error == ""
    assert form["url"] == "http://127.0.0.1/x"
    assert form["method"] == "POST"
    assert "X-A: 1" in form["headers"]
    assert isinstance(notes, list)


def test_curl_import_reports_garbage(pages: WebPages) -> None:
    _, _, error = pages.import_curl({"curl": "wget http://127.0.0.1/x"})
    assert error


def test_config_page_lists_the_editable_fields(pages: WebPages) -> None:
    html = pages.config_page(USER, CSRF)
    assert "SERVER.port" in html or "gRPC" in html


def test_saving_config_writes_the_file(pages: WebPages, config_file: Path) -> None:
    form = {"tab": "basic", "SERVER.max_workers": "16", "__present__SERVER.max_workers": "1"}
    html = pages.save_config(form, USER, CSRF)

    assert "已写回" in html
    assert "max_workers = 16" in config_file.read_text(encoding="utf-8")


def test_saving_nothing_is_refused(pages: WebPages) -> None:
    html = pages.save_config({"tab": "basic"}, USER, CSRF)
    assert "没有可保存的改动" in html


def test_an_invalid_value_is_rejected_without_writing(pages: WebPages, config_file: Path) -> None:
    before = config_file.read_text(encoding="utf-8")
    html = pages.save_config(
        {"tab": "basic", "SERVER.max_workers": "-3", "__present__SERVER.max_workers": "1"}, USER, CSRF
    )

    assert config_file.read_text(encoding="utf-8") == before
    assert "<!doctype html>" in html


def test_adding_and_removing_a_node_updates_the_file(pages: WebPages, config_file: Path) -> None:
    added = pages.add_node({"new_node_id": "n9", "new_node_host": "10.0.0.9", "new_node_port": "9601"}, USER, CSRF)
    assert "<!doctype html>" in added
    assert "10.0.0.9:9601" in config_file.read_text(encoding="utf-8")

    _ = pages.remove_node({"remove_node": "n9"}, USER, CSRF)
    assert "10.0.0.9:9601" not in config_file.read_text(encoding="utf-8")


def test_adding_a_node_without_a_host_is_refused(pages: WebPages, config_file: Path) -> None:
    before = config_file.read_text(encoding="utf-8")
    html = pages.add_node({"new_node_id": "n9"}, USER, CSRF)

    assert "请填 IP 或主机名" in html
    assert config_file.read_text(encoding="utf-8") == before


def test_a_duplicate_node_id_is_refused(pages: WebPages) -> None:
    _ = pages.add_node({"new_node_id": "n9", "new_node_host": "10.0.0.9", "new_node_port": "9601"}, USER, CSRF)
    html = pages.add_node({"new_node_id": "n9", "new_node_host": "10.0.0.10", "new_node_port": "9601"}, USER, CSRF)

    assert "已经有一个 id" in html


def test_a_generated_secret_is_handed_out_exactly_once(pages: WebPages) -> None:
    token = pages.generate_secret("IPCLICK_AUTH_TOKEN")
    assert token

    payload = pages.take_generated(token)
    assert payload is not None
    assert payload["env"] == "IPCLICK_AUTH_TOKEN"
    assert len(payload["value"]) >= 32
    assert pages.take_generated(token) is None


def test_only_declared_secrets_can_be_generated(pages: WebPages) -> None:
    assert pages.generate_secret("IPCLICK_NOT_A_SECRET") == ""
    assert pages.take_generated("") is None


def test_components_page_renders(pages: WebPages) -> None:
    assert "<!doctype html>" in pages.components_page(USER, CSRF)


def test_skill_page_and_markdown(pages: WebPages) -> None:
    assert "<!doctype html>" in pages.skill_page(USER, CSRF)
    assert "name: ipclick" in pages.skill_markdown()


def test_dashboard_extras_expose_the_recorder_state(pages: WebPages) -> None:
    extras = pages.dashboard_extras()
    assert "config_path" in extras
    assert "trace" in extras


def test_deploy_needs_a_known_node(pages: WebPages) -> None:
    assert pages.deploy_plan("nope") is None
    assert pages.deploy_page("nope", USER, CSRF) is None


def test_probing_an_unknown_node_is_reported(pages: WebPages) -> None:
    outcome: dict[str, Any] = pages.probe_node("nope")
    assert outcome.get("ok") is False
