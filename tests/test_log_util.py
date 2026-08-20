from __future__ import annotations

import itertools
import threading
import time

from loguru import logger
import pytest

from ipclick.utils.log_util import LogUtil


def test_concurrent_initialization_does_not_leave_duplicate_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    state_lock = threading.Lock()
    start = threading.Barrier(4)
    identifiers = itertools.count(1)
    active: set[int] = set()
    concurrent_adds = 0
    peak_adds = 0
    errors: list[BaseException] = []

    def fake_add(*_args: object, **_kwargs: object) -> int:
        nonlocal concurrent_adds, peak_adds
        with state_lock:
            concurrent_adds += 1
            peak_adds = max(peak_adds, concurrent_adds)
        time.sleep(0.02)
        handler_id = next(identifiers)
        with state_lock:
            active.add(handler_id)
            concurrent_adds -= 1
        return handler_id

    def fake_remove(handler_id: int) -> None:
        with state_lock:
            active.discard(handler_id)

    monkeypatch.setattr(logger, "add", fake_add)
    monkeypatch.setattr(logger, "remove", fake_remove)
    monkeypatch.setattr(LogUtil, "_configurations", {})
    monkeypatch.setattr(LogUtil, "_own_handler_ids", set())
    monkeypatch.setattr(LogUtil, "_dropped_default_handler", True)
    monkeypatch.setattr(LogUtil, "_emitter", None)

    def initialize() -> None:
        try:
            start.wait(timeout=1.0)
            LogUtil.init(logger_name="race")
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=initialize) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2.0)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert peak_adds == 1
    assert active == set(LogUtil._configurations["race"]["handler_ids"])
