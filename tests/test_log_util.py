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


@pytest.mark.parametrize(
    "rotation",
    [
        {"max_backups": "30 days"},
        {"max_backups": "abc"},
        {"max_size": "100MB"},
        {"max_size": None},
    ],
)
def test_bad_rotation_values_do_not_kill_startup(rotation: dict[str, object]) -> None:
    """日志配置写错不该让服务起不来。

    这两项原来是裸 int() 和裸 f"{...} MB"：max_backups = "30 days"（loguru 自己认的
    保留期写法）会抛 ValueError，而调用点一个在 IPClickServer.__init__（启动直接死，
    报错里还不提是哪个键），一个在 Web 端改日志级别时（500）。仓里其他配置读取一律
    走 as_int 并告警回落。
    """
    LogUtil.init_from_config({"level": "INFO", "rotation": rotation}, debug=False)

    logger.info("仍然可用")
