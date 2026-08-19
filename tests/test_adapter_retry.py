from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest

from ipclick.adapters import retry as retry_module
from ipclick.adapters.base import normalize_js
from ipclick.adapters.retry import RetryPolicy, aretry, retry
from ipclick.adapters.settings import AdapterSettings
from ipclick.dto.response import Response
from ipclick.exceptions import AdapterError, ValidationError
from ipclick.trace import get_recorder

from .helpers import FakeAsyncClock, FakeClock


Outcome = int | Exception


class FakeAdapter:
    adapter_name: str = "fake"

    def __init__(self, outcomes: Iterable[Outcome], settings: AdapterSettings | None = None) -> None:
        self.settings: AdapterSettings = settings or AdapterSettings()
        self.max_retries: int = 3
        self.retry_delay: float = 1.0
        self.outcomes: list[Outcome] = list(outcomes)
        self.calls: int = 0

    def _next(self, url: str) -> Response:
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else 200
        if isinstance(outcome, Exception):
            raise outcome
        return Response(url=url, status_code=outcome, content=b"body")

    @retry()
    def download(self, url: str, **_kwargs: Any) -> Response:
        return self._next(url)

    @aretry()
    async def adownload(self, url: str, **_kwargs: Any) -> Response:
        return self._next(url)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(retry_module, "time", fake)
    return fake


@pytest.fixture
def async_clock(monkeypatch: pytest.MonkeyPatch) -> FakeAsyncClock:
    fake = FakeAsyncClock()
    monkeypatch.setattr(retry_module, "asyncio", fake)
    return fake


def test_success_is_not_retried(clock: FakeClock) -> None:
    adapter = FakeAdapter([200])
    response = adapter.download("http://e.com")

    assert adapter.calls == 1
    assert response.status_code == 200
    assert response.attempts == 1
    assert clock.slept == []


def test_retryable_status_is_retried_until_it_succeeds(clock: FakeClock) -> None:
    adapter = FakeAdapter([503, 200])
    response = adapter.download("http://e.com")

    assert adapter.calls == 2
    assert response.status_code == 200
    assert response.attempts == 2
    assert len(clock.slept) == 1
    assert get_recorder().counters.retries == {"fake:status_code": 1}


def test_allowed_status_codes_suppress_the_retry(clock: FakeClock) -> None:
    adapter = FakeAdapter([503, 200])
    response = adapter.download("http://e.com", allowed_status_codes=[503])

    assert adapter.calls == 1
    assert response.status_code == 503
    assert clock.slept == []


@pytest.mark.usefixtures("clock")
def test_non_retryable_status_is_returned_as_is() -> None:
    adapter = FakeAdapter([404, 200])
    assert adapter.download("http://e.com").status_code == 404
    assert adapter.calls == 1


def test_retries_are_capped(clock: FakeClock) -> None:
    adapter = FakeAdapter([503, 503, 503, 503])
    response = adapter.download("http://e.com", max_retries=2)

    assert adapter.calls == 3
    assert response.status_code == 503
    assert response.attempts == 3
    assert len(clock.slept) == 2


@pytest.mark.usefixtures("clock")
def test_zero_retries_means_one_attempt() -> None:
    adapter = FakeAdapter([503])
    assert adapter.download("http://e.com", max_retries=0).status_code == 503
    assert adapter.calls == 1


@pytest.mark.usefixtures("clock")
def test_exception_is_retried_then_wrapped_into_a_response() -> None:
    boom = RuntimeError("connection reset")
    adapter = FakeAdapter([boom, boom])
    response = adapter.download("http://e.com", max_retries=1)

    assert adapter.calls == 2
    assert response.status_code == -1
    assert response.exception is boom
    assert response.attempts == 2
    assert not response.ok
    assert get_recorder().counters.retries == {"fake:exception": 1}


@pytest.mark.usefixtures("clock")
def test_exception_then_success() -> None:
    adapter = FakeAdapter([RuntimeError("flaky"), 200])
    assert adapter.download("http://e.com").status_code == 200
    assert adapter.calls == 2


@pytest.mark.parametrize("error", [ValidationError("bad url"), AdapterError("missing dependency")])
def test_permanent_errors_are_not_retried(clock: FakeClock, error: Exception) -> None:
    adapter = FakeAdapter([error])
    with pytest.raises(type(error)):
        adapter.download("http://e.com")
    assert adapter.calls == 1
    assert clock.slept == []


def test_backoff_grows_exponentially_within_the_jitter_band(clock: FakeClock) -> None:
    adapter = FakeAdapter([503, 503, 503, 200], AdapterSettings(initial_backoff=1.0, backoff_exponent=2.0))
    adapter.download("http://e.com", max_retries=3, retry_delay=1.0)

    assert len(clock.slept) == 3
    for attempt, delay in enumerate(clock.slept):
        expected = 1.0 * 2.0**attempt
        assert 0.8 * expected <= delay <= 1.2 * expected


def test_backoff_is_capped_by_max_backoff(clock: FakeClock) -> None:
    adapter = FakeAdapter([503] * 6, AdapterSettings(initial_backoff=10.0, max_backoff=12.0))
    adapter.download("http://e.com", max_retries=5, retry_delay=10.0)

    assert max(clock.slept) <= 12.0 * 1.2


def test_retry_delay_can_be_a_random_range(clock: FakeClock) -> None:
    adapter = FakeAdapter([503, 200])
    adapter.download("http://e.com", retry_delay=(2.0, 4.0))

    assert 2.0 * 0.8 <= clock.slept[0] <= 4.0 * 1.2


async def test_async_retry_mirrors_the_sync_behaviour(async_clock: FakeAsyncClock) -> None:
    adapter = FakeAdapter([503, RuntimeError("flaky"), 200])
    response = await adapter.adownload("http://e.com")

    assert adapter.calls == 3
    assert response.status_code == 200
    assert response.attempts == 3
    assert len(async_clock.slept) == 2
    assert get_recorder().counters.retries == {"fake:status_code": 1, "fake:exception": 1}


async def test_async_permanent_error_is_not_retried(async_clock: FakeAsyncClock) -> None:
    adapter = FakeAdapter([ValidationError("bad")])
    with pytest.raises(ValidationError):
        await adapter.adownload("http://e.com")
    assert adapter.calls == 1
    assert async_clock.slept == []


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("", ""),
        ("() => 1", "() => 1"),
        ("async () => 1", "async () => 1"),
        ("function f() {}", "function f() {}"),
        ("document.title", "() => (document.title)"),
        ("return document.title", "() => { return document.title }"),
    ],
)
def test_normalize_js(script: str, expected: str) -> None:
    assert normalize_js(script) == expected


def test_policy_reads_the_adapter_then_the_call_kwargs() -> None:
    adapter = FakeAdapter([], AdapterSettings(max_attempts=9, initial_backoff=3.0, backoff_exponent=1.5))
    adapter.max_retries = 5
    adapter.retry_delay = 2.0

    from_adapter = RetryPolicy.resolve(adapter, {})
    assert (from_adapter.max_retries, from_adapter.base_delay) == (5, 2.0)
    assert (from_adapter.exponent, from_adapter.max_backoff) == (1.5, 30.0)

    from_call = RetryPolicy.resolve(adapter, {"max_retries": 1, "retry_delay": 0.5})
    assert (from_call.max_retries, from_call.base_delay) == (1, 0.5)


def test_policy_normalises_hostile_values() -> None:
    adapter = FakeAdapter([])
    assert RetryPolicy.resolve(adapter, {"max_retries": -3}).max_retries == 0
    assert RetryPolicy.resolve(adapter, {"max_retries": "many"}).max_retries == 3
    assert RetryPolicy.resolve(adapter, {"retry_delay": (1.0, 1.0)}).base_delay == 1.0
    assert RetryPolicy.resolve(adapter, {"retry_delay": (1.0,)}).base_delay == 1.0


def test_policy_decides_which_statuses_are_worth_retrying() -> None:
    policy = RetryPolicy(retry_codes=frozenset({503}), allowed_status_codes=frozenset({429}))
    assert policy.retries_status(503) is True
    assert policy.retries_status(404) is False
    assert policy.retries_status(None) is False
    assert RetryPolicy(retry_codes=frozenset({429}), allowed_status_codes=frozenset({429})).retries_status(429) is False


def test_policy_backoff_is_capped_and_jittered() -> None:
    policy = RetryPolicy(base_delay=10.0, exponent=2.0, max_backoff=25.0)
    assert 8.0 <= policy.delay_for(0) <= 12.0
    assert 20.0 <= policy.delay_for(5) <= 30.0
    assert policy.total_attempts == 4
