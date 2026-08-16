"""日志工具：不能干扰宿主应用，也不能重复输出。"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from ipclick.utils.log_util import LogUtil, SQLiteAdapter, logger


@pytest.fixture(autouse=True)
def _isolate_logger() -> Iterator[None]:
    """每个用例前后都还原 LogUtil 的状态，避免相互影响。"""
    saved_configs = dict(LogUtil._configurations)
    saved_dropped = LogUtil._dropped_default_handler
    LogUtil._configurations.clear()
    yield
    for cfg in LogUtil._configurations.values():
        LogUtil._remove_handlers(cfg["handler_ids"])
    LogUtil._configurations.clear()
    LogUtil._configurations.update(saved_configs)
    LogUtil._dropped_default_handler = saved_dropped


class TestNoDuplicateOutput:
    def test_message_logged_exactly_once(self):
        """回归：loguru 自带的默认 handler 没摘掉时，每条日志会打印两遍。"""
        messages: list[str] = []
        LogUtil.init(level="INFO")
        sink_id = logger.add(messages.append, level="INFO", format="{message}")
        try:
            LogUtil.info("hello-once")
        finally:
            logger.remove(sink_id)

        assert [m.strip() for m in messages].count("hello-once") == 1

    def test_level_filter_is_respected(self):
        """回归：loguru 默认 handler 是 DEBUG 级，会绕过这里设定的 INFO。"""
        messages: list[str] = []
        LogUtil.init(level="INFO")
        sink_id = logger.add(messages.append, level="INFO", format="{message}")
        try:
            LogUtil.debug("should-not-appear")
            LogUtil.info("should-appear")
        finally:
            logger.remove(sink_id)

        joined = " ".join(messages)
        assert "should-not-appear" not in joined
        assert "should-appear" in joined


class TestDoesNotClobberHostHandlers:
    def test_importing_ipclick_keeps_host_handlers(self):
        """回归：模块级 logger.remove() 会把宿主应用配好的 handler 一起清掉。"""
        host_messages: list[str] = []
        host_sink = logger.add(host_messages.append, level="INFO", format="{message}")
        try:
            import importlib

            import ipclick.utils.log_util as module

            importlib.reload(module)
            logger.info("host-handler-still-alive")
        finally:
            logger.remove(host_sink)

        assert any("host-handler-still-alive" in m for m in host_messages)

    def test_init_does_not_remove_foreign_handlers(self):
        host_messages: list[str] = []
        host_sink = logger.add(host_messages.append, level="INFO", format="{message}")
        try:
            LogUtil.init(level="INFO")
            logger.info("still-here")
        finally:
            logger.remove(host_sink)

        assert any("still-here" in m for m in host_messages)


class TestReconfigure:
    def test_reinit_replaces_own_handlers(self):
        LogUtil.init(level="INFO")
        first = list(LogUtil._configurations["default"]["handler_ids"])
        LogUtil.init(level="WARNING")
        second = list(LogUtil._configurations["default"]["handler_ids"])
        assert first != second

    def test_remove_logger(self):
        LogUtil.init(level="INFO", logger_name="temp")
        assert "temp" in LogUtil._configurations
        LogUtil.remove_logger("temp")
        assert "temp" not in LogUtil._configurations

    def test_file_output(self, tmp_path: Path):
        log_file = tmp_path / "app.log"
        LogUtil.init(level="INFO", log_file=log_file)
        LogUtil.info("to-file")
        LogUtil.remove_logger("default")
        assert log_file.exists()
        assert "to-file" in log_file.read_text(encoding="utf-8")

    def test_file_without_suffix_gets_log_extension(self, tmp_path: Path):
        LogUtil.init(level="INFO", log_file=tmp_path / "noext")
        LogUtil.info("x")
        LogUtil.remove_logger("default")
        assert (tmp_path / "noext.log").exists()


class TestOutputAsDirectory:
    """``output`` 填目录时不能把目录名改写成同级文件名。

    回归的是一个静默错误：``logsss/`` 本意是"写进这个目录"，旧实现看它没有
    扩展名就当成文件名，``with_suffix(".log")`` 把最后一段整个换掉，于是日志
    落在了 ``logsss.log``——和目录同级，目录里空空如也。不报任何错。
    """

    def test_trailing_slash_is_a_directory(self, tmp_path: Path):
        target = tmp_path / "logsss"
        LogUtil.init(level="INFO", log_file=f"{target}/")
        LogUtil.info("into-dir")
        LogUtil.remove_logger("default")

        assert target.is_dir()
        # 关键断言：不能在 logsss/ 的**同级**冒出一个 logsss.log
        assert not (tmp_path / "logsss.log").exists()
        written = list(target.glob("*.log"))
        assert written, f"{target} 里没有日志文件"
        assert "into-dir" in written[0].read_text(encoding="utf-8")

    def test_existing_directory_without_trailing_slash(self, tmp_path: Path):
        target = tmp_path / "already-there"
        target.mkdir()
        LogUtil.init(level="INFO", log_file=str(target))
        LogUtil.info("into-existing-dir")
        LogUtil.remove_logger("default")

        assert not (tmp_path / "already-there.log").exists()
        assert list(target.glob("*.log"))

    def test_explicit_file_path_unchanged(self, tmp_path: Path):
        """另一半必须不变：明确写了文件名就还是那个文件。"""
        target = tmp_path / "xxx" / "app.log"
        LogUtil.init(level="INFO", log_file=str(target))
        LogUtil.info("named-file")
        LogUtil.remove_logger("default")

        assert target.exists()
        assert "named-file" in target.read_text(encoding="utf-8")

    def test_directory_from_config(self, tmp_path: Path):
        """走 [LOG].output 这条路（用户实际配置的入口）也得对。"""
        target = tmp_path / "conf-logs"
        LogUtil.init_from_config({"level": "INFO", "output": f"{target}/"})
        LogUtil.info("from-config-dir")
        LogUtil.remove_logger("default")

        assert not (tmp_path / "conf-logs.log").exists()
        assert list(target.glob("*.log"))

    @pytest.mark.parametrize(
        ("raw", "is_dir"),
        [
            ("logs/", True),
            ("logs/app.log", False),
            ("logs/noext", False),
            ("app.log", False),
            ("", False),
        ],
    )
    def test_looks_like_directory(self, raw: str, is_dir: bool):
        from ipclick.utils.path_util import PathUtil

        assert PathUtil.looks_like_directory(raw) is is_dir

    def test_resolve_log_file_variants(self, tmp_path: Path):
        from ipclick.utils.path_util import DEFAULT_LOG_FILENAME, PathUtil

        # 目录写法 -> 目录内的默认文件名
        assert PathUtil.resolve_log_file(f"{tmp_path}/logs/") == tmp_path / "logs" / DEFAULT_LOG_FILENAME
        # 明确的文件名 -> 原样
        assert PathUtil.resolve_log_file(f"{tmp_path}/logs/app.log") == tmp_path / "logs" / "app.log"
        # 没有扩展名的文件名 -> 补 .log（旧行为，保持不变）
        assert PathUtil.resolve_log_file(f"{tmp_path}/logs/noext") == tmp_path / "logs" / "noext.log"


class TestInitFromConfig:
    def test_reads_level_from_config(self):
        """回归：配置文件的 [LOG] 节以前完全没被读取过。"""
        LogUtil.init_from_config({"level": "WARNING", "output": "stdout"})
        assert LogUtil._configurations["default"]["level"] == "WARNING"

    def test_file_output_from_config(self, tmp_path: Path):
        target = tmp_path / "from_config.log"
        LogUtil.init_from_config(
            {"level": "INFO", "output": str(target), "rotation": {"max_size": 5, "max_backups": 2}}
        )
        LogUtil.info("configured")
        LogUtil.remove_logger("default")
        assert target.exists()

    def test_empty_config_uses_defaults(self):
        LogUtil.init_from_config(None)
        assert LogUtil._configurations["default"]["level"] == "INFO"


class TestSQLiteAdapter:
    def test_rejects_unsafe_table_name(self, tmp_path: Path):
        """表名会被拼进 SQL，必须限制成合法标识符。"""
        with pytest.raises(ValueError, match="非法的表名"):
            SQLiteAdapter(str(tmp_path / "a.db"), table_name="logs; DROP TABLE users--")

    def test_relative_path_resolved(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """回归：算出了 resolve_path 却仍用原始 db_path 建连接。"""
        monkeypatch.chdir(tmp_path)
        adapter = SQLiteAdapter("logs/app.db")
        try:
            assert (tmp_path / "logs" / "app.db").exists()
        finally:
            adapter.close()

    def test_writes_records(self, tmp_path: Path):
        db = tmp_path / "app.db"
        adapter = SQLiteAdapter(str(db))
        try:
            LogUtil.init(level="INFO", adapter=adapter)
            LogUtil.info("persisted-message")
            LogUtil.remove_logger("default")

            import sqlite3
            import time

            time.sleep(0.3)  # adapter 走 enqueue=True，异步落库
            rows = sqlite3.connect(db).execute("SELECT message FROM logs").fetchall()
            assert any("persisted-message" in r[0] for r in rows)
        finally:
            adapter.close()
