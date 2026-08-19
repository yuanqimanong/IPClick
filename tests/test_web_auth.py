from __future__ import annotations

import pytest

from ipclick.web.auth import LOCKOUT_WINDOW, MAX_FAILED_ATTEMPTS, SessionStore


def test_lockout_window_starts_at_the_threshold_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 0.0
    monkeypatch.setattr("ipclick.web.auth.time.monotonic", lambda: now)
    store = SessionStore()

    for _ in range(MAX_FAILED_ATTEMPTS - 1):
        store.record_failure("client")
    assert store.is_locked("client") == 0.0

    # 临近原统计窗口末尾的第五次失败，仍应得到完整锁定期。
    now = LOCKOUT_WINDOW - 1
    store.record_failure("client")
    assert store.is_locked("client") == pytest.approx(LOCKOUT_WINDOW)

    now += 1
    assert store.is_locked("client") == pytest.approx(LOCKOUT_WINDOW - 1)


def test_success_clears_failed_login_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ipclick.web.auth.time.monotonic", lambda: 10.0)
    store = SessionStore()
    for _ in range(MAX_FAILED_ATTEMPTS):
        store.record_failure("client")
    assert store.is_locked("client") > 0

    store.record_success("client")
    assert store.is_locked("client") == 0.0
