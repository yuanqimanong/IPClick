from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
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


def test_concurrent_config_saves_preserve_both_updates(
    pages: WebPages, config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ipclick.config_loader import writer

    real_save = writer.save
    first_save_entered = threading.Event()
    release_first_save = threading.Event()
    count_lock = threading.Lock()
    save_count = 0

    def slow_first_save(*args: Any, **kwargs: Any) -> Path | None:
        nonlocal save_count
        with count_lock:
            save_count += 1
            is_first = save_count == 1
        if is_first:
            first_save_entered.set()
            assert release_first_save.wait(timeout=5)
        return real_save(*args, **kwargs)

    monkeypatch.setattr(writer, "save", slow_first_save)
    first_form = {"tab": "basic", "SERVER.host": "0.0.0.0", "__present__SERVER.host": "1"}
    second_form = {"tab": "basic", "SERVER.max_workers": "16", "__present__SERVER.max_workers": "1"}

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(pages.save_config, first_form, USER, CSRF)
        assert first_save_entered.wait(timeout=5)
        second = pool.submit(pages.save_config, second_form, USER, CSRF)
        # 第二个事务必须停在配置锁外，不能读取第一个事务尚未落盘的旧快照。
        assert not second.done()
        release_first_save.set()
        _ = first.result(timeout=5)
        _ = second.result(timeout=5)

    text = config_file.read_text(encoding="utf-8")
    assert 'host = "0.0.0.0"' in text
    assert "max_workers = 16" in text


def test_cluster_hot_reload_is_deferred_in_multiprocess_mode(pages: WebPages, monkeypatch: pytest.MonkeyPatch) -> None:
    from ipclick import server_settings

    called = False

    def reload_cluster() -> tuple[bool, str]:
        nonlocal called
        called = True
        return True, "reloaded"

    pages.ctx.config["SERVER"]["processes"] = 2
    pages.ctx._on_cluster_changed = reload_cluster
    monkeypatch.setattr(server_settings, "resolve_processes", lambda _configured: 2)

    pages.ctx.hot_reload_cluster()
    messages, errors = pages.ctx.take_flash()

    assert called is False
    assert errors == []
    assert any("全部 worker" in message and "重启" in message for message in messages)


def test_saving_nothing_is_refused(pages: WebPages) -> None:
    html = pages.save_config({"tab": "basic"}, USER, CSRF)
    assert "没有可保存的改动" in html


def test_saving_the_cluster_tab_untouched_is_refused(pages: WebPages) -> None:
    """集群页什么都不动直接按保存，不该报成一项改动。

    ``CLUSTER.forward`` 原来是绕过"只报真正变了的项"那一层无条件写进 updates 的，
    于是这一页每次保存都报"已写回（1 项）"外加"这些项要重启 ipclick 才生效"。
    那句提示打多了就没人看了，而它在真需要重启时是唯一的信号。
    """
    html = pages.save_config({"tab": "cluster", "__present__CLUSTER.forward_on": "1"}, USER, CSRF)

    assert "没有可保存的改动" in html
    # 页面上有一句静态说明也含"要重启"，所以比对的是真正那条提示的措辞。
    assert "这些项要重启" not in html


def test_toggling_cluster_forwarding_is_still_reported(pages: WebPages, config_file: Path) -> None:
    """真的把转发打开时，改动和重启提示都要照常给出。"""
    html = pages.save_config(
        {"tab": "cluster", "__present__CLUSTER.forward_on": "1", "CLUSTER.forward_on": "on"}, USER, CSRF
    )

    assert "已写回" in html
    assert "服务端转发" in html
    assert 'forward = "on"' in config_file.read_text(encoding="utf-8")


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
