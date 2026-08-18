"""安装任务的进度解析与输出折叠。

守的是"用户能看出它在干活"这件事：camoufox 的本体要下十几分钟，那段时间里
"在下载"和"卡死了"在页面上必须长得不一样。
"""

from __future__ import annotations

import io

from ipclick.web.installer import (
    Job,
    Plan,
    _iter_progress_lines,  # pyright: ignore[reportPrivateUsage]
    _parse_percent,  # pyright: ignore[reportPrivateUsage]
    _parse_phase,  # pyright: ignore[reportPrivateUsage]
    strip_ansi,
)


class TestPercent:
    def test_explicit_percent(self):
        """playwright 与 pip 都直接写百分号。"""
        assert _parse_percent("Downloading Chromium |████ | 45% of 168.5 MiB") == 45.0

    def test_ratio_without_percent(self):
        """camoufox 用 rich 的 DownloadColumn，渲染出来**只有分子分母**。
        只认 % 的话，那 1 GB 全程解析不到任何进度——而它恰恰最需要进度。"""
        assert _parse_percent("⠹ Downloading ━━━━ 0.5/1.0 GB 2.4 MB/s") == 50.0

    def test_url_escape_is_not_progress(self):
        """URL 里的 %E8 之类不能被当成百分比。"""
        assert _parse_percent("fetching https://x/?q=%E8%AF%95") is None

    def test_out_of_range_rejected(self):
        assert _parse_percent("150%") is None

    def test_ratio_needs_a_unit(self):
        """没有单位的 a/b 可能是任何东西（版本号、路径），不猜。"""
        assert _parse_percent("Extracting 1200/4000") is None

    def test_plain_line(self):
        assert _parse_percent("Requirement already satisfied: certifi in ./x (2026.7.22)") is None

    def test_phase_labels(self):
        assert _parse_phase("⠹ Downloading ━━ 0.5/1.0 GB") == "下载中"
        assert _parse_phase("⠸ Extracting ━━ 62%") == "解压中"
        assert _parse_phase("some other line") == ""


class TestStreamSplitting:
    def test_splits_on_carriage_return(self):
        """进度条靠 \\r 原地重画，整段下载只产生一行。按 \\n 读的话，最需要看到
        进度的那十几分钟里什么都收不到。"""
        assert list(_iter_progress_lines(io.StringIO("a\rb\rc\nlast"))) == ["a", "b", "c", "last"]

    def test_blank_segments_dropped(self):
        assert list(_iter_progress_lines(io.StringIO("\r\n  \nx\n"))) == ["x"]

    def test_strip_ansi(self):
        assert strip_ansi("\x1b[38;5;2m━━━\x1b[0m 45.2%") == "━━━ 45.2%"

    def test_strip_ansi_handles_cursor_codes(self):
        assert strip_ansi("\x1b[?25l\x1b[2Kdone\x1b[?25h") == "done"


class TestJobOutput:
    @staticmethod
    def _job() -> Job:
        return Job(id="j", title="t", command=("echo",))

    def test_progress_lines_replace_instead_of_accumulate(self):
        """一次下载能刷出上万次更新。全留着的话真正有用的报错会被挤出保留窗口，
        而那是出问题时唯一要看的东西。"""
        job = self._job()
        job.append("starting")
        for percent in range(0, 100, 5):
            job.append(f"Downloading ━━━ {percent}%")
        output = job.output()
        assert output[0] == "starting"
        assert len(output) == 2, output
        assert output[-1].endswith("95%")

    def test_non_progress_lines_are_kept(self):
        job = self._job()
        job.append("Downloading ━━━ 10%")
        job.append("done")
        job.append("Extracting ━━━ 20%")
        assert job.output() == ["Downloading ━━━ 10%", "done", "Extracting ━━━ 20%"]

    def test_progress_reaches_the_snapshot(self):
        job = self._job()
        job.append("⠹ Downloading ━━━ 0.5/1.0 GB")
        progress = job.snapshot()["progress"]
        assert progress["percent"] == 50.0
        assert progress["phase"] == "下载中"

    def test_snapshot_has_progress_even_before_any_output(self):
        """前端每次轮询都读这个字段，缺了它就得写一堆判空。"""
        assert self._job().snapshot()["progress"]["percent"] is None

    def test_plan_is_not_serialised(self):
        """snapshot 是给页面的，里面不该有内部对象。"""
        from ipclick.components import BY_EXTRA

        job = Job(id="j", title="t", command=("x",), plan=Plan("browser", BY_EXTRA["camoufox"], "t", ("x",)))
        assert "plan" not in job.snapshot()


class TestChildEnv:
    def test_force_color_is_set(self):
        """rich 检测到 stdout 不是终端就不再实时刷新——收进管道之后我们只能看到
        首尾两段，中间十几分钟一片空白。FORCE_COLOR 让它照常输出。"""
        from ipclick.web.installer import _child_env  # pyright: ignore[reportPrivateUsage]

        env = _child_env()
        assert env["FORCE_COLOR"] == "1"
        # pip 的进度条方向相反：没有有用信息，只会刷屏
        assert env["PIP_PROGRESS_BAR"] == "off"


class TestRevisions:
    def test_playwright_and_patchright_are_counted_separately(self, tmp_path, monkeypatch):
        """两者共用 ms-playwright 目录，但钉的 chromium revision 不同。
        对它们报整个目录的大小是错的——两个组件会显示同一个数。"""
        from ipclick.components import BY_EXTRA
        from ipclick.web.installer import browser_body_location

        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
        for name, size in (("chromium-1223", 3), ("chromium-1228", 5), ("unrelated", 9)):
            directory = tmp_path / name
            directory.mkdir()
            (directory / "blob").write_bytes(b"x" * size * 1000)

        play_dirs, play_size = browser_body_location(BY_EXTRA["playwright"])
        patch_dirs, patch_size = browser_body_location(BY_EXTRA["patchright"])
        assert "chromium-1223" in play_dirs and "chromium-1228" not in play_dirs
        assert "chromium-1228" in patch_dirs and "chromium-1223" not in patch_dirs
        assert play_size != patch_size
        assert "unrelated" not in play_dirs + patch_dirs


class TestSizeSampler:
    def test_reports_delta_not_total(self, tmp_path, monkeypatch):
        """playwright 与 patchright 共用 ms-playwright，里面可能已经躺着另一个的
        600 MB。报总量的话，patchright 刚开始下就显示"已下载 638 MB"——那个数字
        比它要下的东西还大。"""
        import threading
        import time

        from ipclick.components import BY_EXTRA
        from ipclick.web.installer import InstallManager, Plan

        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
        (tmp_path / "chromium-1223").mkdir()
        (tmp_path / "chromium-1223" / "old").write_bytes(b"x" * 5000)  # 别人早就装好的

        job = Job(
            id="j",
            title="t",
            command=("x",),
            plan=Plan("browser", BY_EXTRA["patchright"], "t", ("x",)),
        )
        manager = InstallManager()
        stop = threading.Event()
        monkeypatch.setattr("ipclick.web.installer._SAMPLE_INTERVAL", 0.05)
        watcher = manager._watch_size(job, stop)  # pyright: ignore[reportPrivateUsage]
        assert watcher is not None
        try:
            (tmp_path / "chromium-1228").mkdir()
            (tmp_path / "chromium-1228" / "new").write_bytes(b"y" * 2000)
            deadline = time.monotonic() + 3
            while job.progress.done_bytes == 0 and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            stop.set()
            watcher.join(timeout=2)

        assert job.progress.done_bytes == 2000, "应该只算这次任务写进去的那部分"

    def test_no_sampler_for_package_installs(self):
        """装 Python 包写的是 site-packages，那个目录里本来就有别的东西，
        量出来的增量没有意义。"""
        import threading

        from ipclick.components import BY_EXTRA
        from ipclick.web.installer import InstallManager, Plan

        job = Job(id="j", title="t", command=("x",), plan=Plan("install", BY_EXTRA["camoufox"], "t", ("x",)))
        assert InstallManager()._watch_size(job, threading.Event()) is None  # pyright: ignore[reportPrivateUsage]


class TestPhaseAcrossLines:
    def test_phase_comes_from_any_line(self):
        """playwright 把 "Downloading Chromium …" 和那条 |■■■| 进度条分成两行。
        只看进度行的话永远读不到阶段名。"""
        job = Job(id="j", title="t", command=("x",))
        job.append("Downloading Chromium 145.0 (playwright build v1223) from https://…")
        job.append("|■■■■     | 10% of 113.2 MiB")
        assert job.progress.phase == "下载中"
        assert job.progress.percent == 10.0

    def test_phase_switches_to_extracting(self):
        """camoufox 下完 1 GB 还要解压 1 GB。两段都显示"进行中"的话，
        进度条走到 100% 又回到 0% 会被当成出错。"""
        job = Job(id="j", title="t", command=("x",))
        job.append("⠹ Downloading ━━━ 1.0/1.0 GB")
        assert job.progress.phase == "下载中"
        job.append("⠸ Extracting ━━━ 5%")
        assert job.progress.phase == "解压中"
