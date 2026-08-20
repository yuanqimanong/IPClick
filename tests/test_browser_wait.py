from __future__ import annotations

import pytest

from ipclick.adapters.browser_adapter import NETWORK_IDLE, _goto_state, _RenderPlan, _settle
from ipclick.adapters.browser_settings import BrowserSettings


class FakePage:
    def __init__(self, fail: bool = False) -> None:
        self.states: list[tuple[str, int]] = []
        self.fail: bool = fail

    async def wait_for_load_state(self, state: str, timeout: int) -> None:
        self.states.append((state, timeout))
        if self.fail:
            raise TimeoutError("no idle window")


def _plan(wait_until: str, *, settle_ms: int = 5000, page_ms: int = 30000) -> _RenderPlan:
    return _RenderPlan(
        url="http://127.0.0.1/x",
        context_options={},
        cookies=[],
        block_resources=(),
        wait_until=wait_until,
        page_timeout_ms=page_ms,
        script_timeout=60.0,
        settle_timeout_ms=settle_ms,
    )


def test_default_wait_until_no_longer_silently_drops_late_content() -> None:
    assert BrowserSettings().wait_until == NETWORK_IDLE
    assert BrowserSettings().settle_timeout == 5.0


def test_settle_timeout_is_configurable() -> None:
    settings = BrowserSettings.from_config({"timeout": {"settle": 2.5}})
    assert settings.settle_timeout == 2.5
    assert BrowserSettings.from_config({"timeout": {"settle": 0}}).settle_timeout == 0.0
    assert BrowserSettings.from_config({"timeout": {"settle": "nonsense"}}).settle_timeout == 5.0


@pytest.mark.parametrize(
    ("wait_until", "expected"),
    [("networkidle", "load"), ("load", "load"), ("domcontentloaded", "domcontentloaded"), ("commit", "commit")],
)
def test_navigation_never_waits_for_idle_directly(wait_until: str, expected: str) -> None:
    assert _goto_state(wait_until) == expected


async def test_idle_is_awaited_after_the_load_event() -> None:
    page = FakePage()
    await _settle(page, _plan(NETWORK_IDLE))
    assert page.states == [(NETWORK_IDLE, 5000)]


async def test_idle_budget_never_exceeds_the_page_timeout() -> None:
    page = FakePage()
    await _settle(page, _plan(NETWORK_IDLE, settle_ms=30000, page_ms=4000))
    assert page.states == [(NETWORK_IDLE, 4000)]


async def test_a_page_that_never_goes_idle_still_returns_content() -> None:
    page = FakePage(fail=True)
    await _settle(page, _plan(NETWORK_IDLE))
    assert page.states == [(NETWORK_IDLE, 5000)]


@pytest.mark.parametrize(("wait_until", "settle_ms"), [("load", 5000), (NETWORK_IDLE, 0)])
async def test_settle_is_skipped_when_not_asked_for(wait_until: str, settle_ms: int) -> None:
    page = FakePage()
    await _settle(page, _plan(wait_until, settle_ms=settle_ms))
    assert page.states == []


def test_unknown_wait_until_falls_back_to_the_default() -> None:
    assert BrowserSettings.from_config({"wait_until": "whenever"}).wait_until == NETWORK_IDLE
    assert BrowserSettings.from_config({"wait_until": " LOAD "}).wait_until == "load"
