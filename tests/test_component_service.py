from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

import grpc
import pytest

from ipclick.dto.proto import task_pb2
from ipclick.services.components import DISABLED_MESSAGE, ComponentService
from ipclick.utils.config_util import Settings

from .helpers import RecordingContext


class FakeInstaller:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.on_finished: Any = None

    def install(self, extra: str) -> tuple[bool, str]:
        self.calls.append(("install", extra))
        return True, f"installed {extra}"

    def uninstall(self, extra: str) -> tuple[bool, str]:
        self.calls.append(("uninstall", extra))
        return True, f"uninstalled {extra}"

    def fetch_browser(self, extra: str, kind: str) -> tuple[bool, str]:
        self.calls.append(("browser", f"{extra}:{kind}"))
        return True, "fetched"

    def current(self) -> dict[str, Any]:
        return {}


@pytest.fixture
def installer() -> FakeInstaller:
    return FakeInstaller()


@pytest.fixture
def service(installer: FakeInstaller, monkeypatch: pytest.MonkeyPatch) -> ComponentService:
    config = Settings({"CLUSTER": {"allow_remote_install": True}, "BROWSER": {"enabled": False}})
    built = ComponentService(config)
    monkeypatch.setattr(built, "installer", lambda: installer)
    return built


def _context() -> tuple[RecordingContext, grpc.ServicerContext]:
    recording = RecordingContext()
    return recording, cast(grpc.ServicerContext, cast(object, recording))


def test_remote_management_is_off_by_default() -> None:
    disabled = ComponentService(Settings({}))
    recording, context = _context()

    response = disabled.handle(task_pb2.ComponentReq(op="list"), context, node_id="n1")

    assert recording.code is grpc.StatusCode.PERMISSION_DENIED
    assert response.ok is False
    assert response.message == DISABLED_MESSAGE
    assert response.node_id == "n1"


@pytest.mark.parametrize("raw", [{"allow_remote_install": True}, {"allow_remote_install": "yes"}])
def test_remote_management_can_be_switched_on(raw: dict[str, object]) -> None:
    assert ComponentService(Settings({"CLUSTER": raw})).enabled is True


@pytest.mark.parametrize("raw", [{}, {"allow_remote_install": False}, {"allow_remote_install": "no"}])
def test_remote_management_stays_off_for_anything_else(raw: dict[str, object]) -> None:
    assert ComponentService(Settings({"CLUSTER": raw})).enabled is False


def test_unknown_operations_are_rejected_before_touching_the_installer(
    service: ComponentService, installer: FakeInstaller
) -> None:
    recording, context = _context()

    response = service.handle(task_pb2.ComponentReq(op="rm -rf"), context, node_id="n1")

    assert recording.code is grpc.StatusCode.INVALID_ARGUMENT
    assert response.ok is False
    assert installer.calls == []


@pytest.mark.parametrize(
    ("op", "expected"),
    [("install", ("install", "niquests")), ("uninstall", ("uninstall", "niquests"))],
)
def test_mutating_operations_reach_the_installer(
    service: ComponentService, installer: FakeInstaller, op: str, expected: tuple[str, str]
) -> None:
    _, context = _context()

    response = service.handle(task_pb2.ComponentReq(op=op, extra="niquests"), context, node_id="n1")

    assert response.ok is True
    assert installer.calls == [expected]


def test_browser_download_defaults_to_chromium(service: ComponentService, installer: FakeInstaller) -> None:
    _, context = _context()

    _ = service.handle(task_pb2.ComponentReq(op="browser", extra="patchright"), context, node_id="n1")
    assert installer.calls == [("browser", "patchright:chromium")]

    installer.calls.clear()
    _ = service.handle(
        task_pb2.ComponentReq(op="browser", extra="camoufox", browser_kind="firefox"), context, node_id="n1"
    )
    assert installer.calls == [("browser", "camoufox:firefox")]


def test_read_only_operations_do_not_run_anything(service: ComponentService, installer: FakeInstaller) -> None:
    _, context = _context()

    for op in ("list", "status"):
        response = service.handle(task_pb2.ComponentReq(op=op), context, node_id="n1")
        assert response.ok is True

    assert installer.calls == []


def test_snapshot_is_json(service: ComponentService) -> None:
    import json

    parsed = json.loads(service.snapshot_json())
    assert isinstance(parsed, list)
    assert all("name" in item for item in parsed)


def test_installer_is_created_once_under_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    import ipclick.web.installer as installer_module

    created: list[FakeInstaller] = []

    def build() -> FakeInstaller:
        manager = FakeInstaller()
        created.append(manager)
        return manager

    monkeypatch.setattr(installer_module, "InstallManager", build)
    component_service = ComponentService(Settings({"CLUSTER": {"allow_remote_install": True}}))

    with ThreadPoolExecutor(max_workers=8) as pool:
        managers = list(pool.map(lambda _index: component_service.installer(), range(32)))

    assert len(created) == 1
    assert all(manager is created[0] for manager in managers)
