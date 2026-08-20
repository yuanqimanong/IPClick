"""Web 端可选组件安装计划、后台作业与浏览器下载进度管理。"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Literal, final

from ipclick.components import BY_EXTRA, Component
from ipclick.utils import module_probe
from ipclick.utils.log_util import log


JobStatus = Literal["running", "succeeded", "failed"]

_OUTPUT_LINES = 300

_TIMEOUT = 45 * 60

_RETAIN = 30 * 60

_SAMPLE_INTERVAL = 1.0


@final
@dataclass
class Progress:
    """后台安装任务的可展示进度。"""

    percent: float | None = None
    done_bytes: int = 0
    speed: float = 0.0
    phase: str = ""

    def snapshot(self) -> dict[str, Any]:
        """返回适合 JSON 序列化的进度快照。"""
        return {
            "percent": round(self.percent, 1) if self.percent is not None else None,
            "done_bytes": self.done_bytes,
            "speed": round(self.speed),
            "phase": self.phase,
        }


@final
@dataclass
class Job:
    """一个受锁保护的后台安装作业及其有限长度输出。"""

    id: str
    title: str
    command: tuple[str, ...]
    status: JobStatus = "running"
    returncode: int | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    progress: Progress = field(default_factory=Progress)
    plan: Plan | None = None
    _output: deque[str] = field(default_factory=lambda: deque(maxlen=_OUTPUT_LINES))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def append(self, line: str) -> None:
        """追加输出并从工具日志中提取阶段和百分比。"""
        percent = _parse_percent(line)
        phase = _parse_phase(line)
        with self._lock:
            if phase:
                self.progress.phase = phase
            if percent is not None:
                self.progress.percent = percent
                if self._output and _parse_percent(self._output[-1]) is not None:
                    self._output[-1] = line
                    return
            self._output.append(line)

    def output(self) -> list[str]:
        """复制当前输出，避免调用方观察到并发修改。"""
        with self._lock:
            return list(self._output)

    def snapshot(self) -> dict[str, Any]:
        """返回任务当前状态的可序列化快照。"""
        return {
            "id": self.id,
            "title": self.title,
            "command": " ".join(self.command),
            "status": self.status,
            "returncode": self.returncode,
            "elapsed": int((self.finished_at or time.time()) - self.started_at),
            "progress": self.progress.snapshot(),
            "output": self.output(),
        }


_PERCENT_RE = re.compile(r"(?:^|[\s|\[（(━█▉▊▋▌▍▎▏]) ?(\d{1,3}(?:\.\d+)?)\s?%")

_RATIO_RE = re.compile(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*([KMGT]?i?B)\b", re.IGNORECASE)

_ANSI_RE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")


def _parse_percent(line: str) -> float | None:
    matches = _PERCENT_RE.findall(line)
    if matches:
        try:
            value = float(matches[-1])
        except ValueError:
            return None
        return value if 0 <= value <= 100 else None

    ratio = _RATIO_RE.search(line)
    if ratio is None:
        return None
    try:
        done, total = float(ratio.group(1)), float(ratio.group(2))
    except ValueError:
        return None
    if total <= 0 or done < 0 or done > total:
        return None
    return done / total * 100


_PHASES: tuple[tuple[str, str], ...] = (
    ("downloading", "下载中"),
    ("extracting", "解压中"),
    ("installing", "安装中"),
    ("unpacking", "解包中"),
)


def _parse_phase(line: str) -> str:
    lowered = line.lower()
    return next((label for key, label in _PHASES if key in lowered), "")


def strip_ansi(text: str) -> str:
    """移除子进程输出中的 ANSI 控制序列。"""
    return _ANSI_RE.sub("", text)


@final
@dataclass(frozen=True)
class Toolchain:
    """指向当前 Python 环境的 pip 或 uv 命令封装。"""

    kind: Literal["pip", "uv"]
    executable: tuple[str, ...]

    def command(self, verb: str, *args: str) -> tuple[str, ...]:
        """构造安装工具命令，并确保 uv 操作当前解释器。"""
        if self.kind == "uv":
            return (*self.executable, verb, "--python", sys.executable, *args)
        return (*self.executable, verb, *args)

    def describe(self) -> str:
        """返回用于管理页展示的工具链说明。"""
        return f"{self.kind}（{' '.join(self.executable)} → {sys.executable}）"


def detect_toolchain() -> Toolchain | None:
    """优先检测当前解释器的 pip，缺失时回落到 PATH 中的 uv。"""
    if module_probe.installed("pip"):
        return Toolchain(kind="pip", executable=(sys.executable, "-m", "pip"))

    uv = shutil.which("uv")
    if uv:
        return Toolchain(kind="uv", executable=(uv, "pip"))
    return None


def manual_hint(component: Component) -> str:
    """生成无法自动安装时可复制执行的命令提示。"""
    return (
        f"本环境既没有 pip 也没有 uv，无法从页面安装。请在装了其中之一的环境里执行："
        f'{sys.executable} -m pip install "ipclick[{component.extra}]"'
        f'，或 uv pip install --python {sys.executable} "ipclick[{component.extra}]"'
    )


def extra_requirements(extra: str) -> tuple[str, ...]:
    """从已安装发行版元数据中提取指定 extra 的直接依赖。"""
    import importlib.metadata

    try:
        declared = importlib.metadata.requires("ipclick") or []
    except importlib.metadata.PackageNotFoundError:
        return ()

    marker = f'extra == "{extra}"'
    found: list[str] = []
    for entry in declared:
        requirement, _, condition = entry.partition(";")
        normalized = condition.replace("'", '"').strip()
        if marker in normalized:
            found.append(requirement.strip())
    return tuple(found)


def _install_targets(component: Component) -> tuple[tuple[str, ...], str]:
    requirements = extra_requirements(component.extra)
    if requirements:
        return requirements, ""
    from ipclick import __version__

    return (
        (f"ipclick[{component.extra}]=={__version__}",),
        f"读不到 ipclick 的依赖元数据，回落到 ipclick[{component.extra}]=={__version__}（需要该版本在索引上可获取）",
    )


InstallOp = Literal["install", "uninstall", "browser"]


@final
@dataclass(frozen=True)
class Plan:
    """经白名单组件和操作校验后的子进程执行计划。"""

    op: InstallOp
    component: Component
    title: str
    command: tuple[str, ...]
    note: str = ""

    @property
    def shell_form(self) -> str:
        """返回仅用于日志展示的命令文本。"""
        return " ".join(self.command)


def plan(op: str, extra: str, *, browser_kind: str = "chromium") -> tuple[Plan | None, str]:
    """为组件安装、卸载或浏览器下载生成受限命令。"""
    component = BY_EXTRA.get((extra or "").strip())
    if component is None:
        known = ", ".join(sorted(BY_EXTRA))
        return None, f"未知的组件 {extra!r}。可选：{known}"

    if op == "browser":
        if not component.browser_command:
            return None, f"{component.name} 用的是本机已装的 Chrome，不需要下载浏览器本体"
        installed, _ = _package_state(component)
        if not installed:
            return None, f"请先安装 {component.name} 的 Python 包，再下载浏览器本体"
        command = _browser_command(component, browser_kind)
        if command is None:
            return None, f"不认识 {component.name} 的浏览器本体安装方式"
        return Plan("browser", component, f"下载 {component.name} 浏览器本体", command), ""

    if op not in ("install", "uninstall"):
        return None, f"未知的动作 {op!r}（可选：install / uninstall / browser）"

    toolchain = detect_toolchain()
    if toolchain is None:
        return None, manual_hint(component)

    if op == "install":
        targets, note = _install_targets(component)
        return Plan("install", component, f"安装 {component.name}", toolchain.command("install", *targets), note), ""

    command = toolchain.command("uninstall", *_uninstall_flags(toolchain), component.distribution)
    return Plan("uninstall", component, f"卸载 {component.name}", command), ""


def _child_env() -> dict[str, str]:
    """构造安装进程环境，并剔除 IPClick 自身的运行时凭据。"""
    from ipclick.secrets import SECRETS

    secret_names = {spec.env.upper() for spec in SECRETS}
    # 保留 PATH、代理和私有包仓库凭据，确保 pip/uv 正常工作；只移除安装
    # 子进程完全不需要的 IPClick 服务令牌、代理密码和 Web 凭据。
    inherited = {key: value for key, value in os.environ.items() if key.upper() not in secret_names}
    return {
        **inherited,
        "PIP_PROGRESS_BAR": "off",
        "PYTHONUNBUFFERED": "1",
        "FORCE_COLOR": "1",
        "COLUMNS": "100",
    }


def _iter_progress_lines(stream: Any) -> Iterator[str]:
    buffer = ""
    while True:
        chunk = stream.read(1)
        if not chunk:
            break
        if chunk in ("\n", "\r"):
            if buffer.strip():
                yield buffer
            buffer = ""
            continue
        buffer += chunk
    if buffer.strip():
        yield buffer


def execute(
    command: tuple[str, ...],
    on_line: Callable[[str], None],
    *,
    timeout: int = _TIMEOUT,
) -> int:
    """同步执行计划，流式回传清理后的输出并强制总超时。"""
    on_line(f"$ {' '.join(command)}")
    timed_out = threading.Event()
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_child_env(),
        )
    except OSError as e:
        on_line(f"无法执行命令：{e}")
        return -1

    def terminate_on_timeout() -> None:
        if process.poll() is not None:
            return
        timed_out.set()
        with suppress(OSError):
            process.kill()

    # ``stdout.read`` 可能永久阻塞；独立 watchdog 才能保证总超时真实生效。
    watchdog = threading.Timer(timeout, terminate_on_timeout)
    watchdog.daemon = True
    watchdog.start()
    try:
        assert process.stdout is not None
        for line in _iter_progress_lines(process.stdout):
            on_line(strip_ansi(line).rstrip())
        returncode = process.wait()
        if not timed_out.is_set():
            return returncode
        on_line(f"超时（{timeout // 60} 分钟）已终止。网络慢的话请在终端里手动执行上面那条命令")
        return -1
    except Exception as e:
        on_line(f"执行出错：{type(e).__name__}: {e}")
        return -1
    finally:
        watchdog.cancel()


class InstallManager:
    """串行调度组件安装任务，并为轮询接口保留近期结果。"""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._current: Job | None = None
        self._lock: threading.Lock = threading.Lock()
        self._seq: int = 0
        self.on_finished: Any = None

    def install(self, extra: str) -> tuple[bool, str]:
        """在后台安装白名单中的 extra。"""
        return self._plan_and_start("install", extra)

    def uninstall(self, extra: str) -> tuple[bool, str]:
        """在后台卸载白名单组件对应的发行包。"""
        return self._plan_and_start("uninstall", extra)

    def fetch_browser(self, extra: str, kind: str = "chromium") -> tuple[bool, str]:
        """在后台下载组件所需的浏览器本体。"""
        return self._plan_and_start("browser", extra, browser_kind=kind)

    def _plan_and_start(self, op: InstallOp, extra: str, *, browser_kind: str = "chromium") -> tuple[bool, str]:
        prepared, reason = plan(op, extra, browser_kind=browser_kind)
        if prepared is None:
            return False, reason
        return self._start(prepared)

    def current(self) -> dict[str, Any] | None:
        """返回运行中的任务，或最近完成任务的快照。"""
        with self._lock:
            self._prune_locked()
            if self._current is not None:
                return self._current.snapshot()
            if not self._jobs:
                return None
            latest = max(self._jobs.values(), key=lambda j: j.started_at)
            return latest.snapshot()

    def get(self, job_id: str) -> dict[str, Any] | None:
        """按任务 id 返回快照。"""
        with self._lock:
            job = self._jobs.get(job_id)
            return job.snapshot() if job else None

    @property
    def busy(self) -> bool:
        """表示当前是否有安装子进程正在运行。"""
        with self._lock:
            return self._current is not None

    @staticmethod
    def _lookup(extra: str) -> Component | None:
        return BY_EXTRA.get((extra or "").strip())

    def _start(self, prepared: Plan) -> tuple[bool, str]:
        with self._lock:
            if self._current is not None:
                return False, f"已有任务在执行：{self._current.title}。装包不能并发，请等它跑完"
            self._seq += 1
            job = Job(id=f"job-{self._seq}", title=prepared.title, command=prepared.command, plan=prepared)
            if prepared.note:
                job.append(f"（{prepared.note}）")
            self._jobs[job.id] = job
            self._current = job

        thread = threading.Thread(target=self._run, args=(job,), name="ipclick-install", daemon=True)
        thread.start()
        log.info(f"Web 端开始执行：{prepared.title} -> {prepared.shell_form}")
        return True, f"{prepared.title} 已在后台开始"

    def _run(self, job: Job) -> None:
        stop = threading.Event()
        watcher = self._watch_size(job, stop)
        try:
            self._finish(job, returncode=execute(job.command, job.append))
        finally:
            stop.set()
            if watcher is not None:
                watcher.join(timeout=2)

    def _watch_size(self, job: Job, stop: threading.Event) -> threading.Thread | None:
        if job.plan is None or job.plan.op != "browser":
            return None
        root = _browser_root(job.plan.component)
        if root is None:
            return None

        baseline = _dir_size(root)

        def sample() -> None:
            last_size, last_at = baseline, time.monotonic()
            while not stop.wait(_SAMPLE_INTERVAL):
                size = _dir_size(root)
                now = time.monotonic()
                elapsed = max(0.001, now - last_at)
                job.progress.done_bytes = max(0, size - baseline)
                job.progress.speed = max(0.0, (size - last_size) / elapsed)
                last_size, last_at = size, now

        thread = threading.Thread(target=sample, name="ipclick-install-size", daemon=True)
        thread.start()
        return thread

    def _finish(self, job: Job, *, returncode: int) -> None:
        job.returncode = returncode
        job.status = "succeeded" if returncode == 0 else "failed"
        job.finished_at = time.time()
        with self._lock:
            if self._current is job:
                self._current = None

        level = log.info if job.status == "succeeded" else log.warning
        level(f"Web 端任务结束：{job.title} -> {job.status}（退出码 {returncode}）")

        callback = self.on_finished
        if callback is not None:
            try:
                callback(job)
            except Exception as e:
                log.warning(f"安装任务的回调失败：{e}")

    def _prune_locked(self) -> None:
        cutoff = time.time() - _RETAIN
        for key in [k for k, v in self._jobs.items() if v.finished_at and v.finished_at < cutoff]:
            del self._jobs[key]


def _uninstall_flags(toolchain: Toolchain) -> tuple[str, ...]:
    return ("-y",) if toolchain.kind == "pip" else ()


def _package_state(component: Component) -> tuple[bool, str | None]:
    from ipclick.components import package_status

    return package_status(component)


_BROWSER_KINDS = frozenset({"chromium", "firefox", "webkit"})


def _browser_command(component: Component, kind: str = "chromium") -> tuple[str, ...] | None:
    if component.extra == "camoufox":
        return (sys.executable, "-m", "camoufox", "fetch")
    if component.extra in ("playwright", "patchright"):
        target = kind if kind in _BROWSER_KINDS else "chromium"
        return (sys.executable, "-m", component.extra, "install", target)
    return None


def browser_body_location(component: Component) -> tuple[str, int]:
    """返回组件当前浏览器本体的位置说明和磁盘占用。"""
    if component.extra in ("playwright", "patchright"):
        entries = playwright_revisions(component.extra)
        if not entries:
            return "", 0
        return " · ".join(path.name for path, _ in entries), sum(size for _, size in entries)

    root = _browser_root(component)
    if root is None or not root.exists():
        return "", 0
    return str(root), _dir_size(root)


def playwright_revisions(engine: str) -> list[tuple[Path, int]]:
    """列出属于当前 Playwright/Patchright 版本的浏览器 revision。"""
    import json

    from ipclick.adapters.browser_engines import playwright_registry_dir

    registry = playwright_registry_dir(engine)
    if registry is None or not registry.exists():
        return []
    try:
        module = __import__(engine)
        manifest = Path(str(module.__file__)).parent / "driver" / "package" / "browsers.json"
        revisions: set[str] = set()
        for entry in json.loads(manifest.read_text(encoding="utf-8")).get("browsers", []):
            name, revision = entry.get("name"), entry.get("revision")
            if not name or not revision:
                continue
            revisions.add(f"{name}-{revision}")
            revisions.add(f"{name.replace('-', '_')}-{revision}")
    except Exception as e:
        log.debug(f"读不到 {engine} 的 browsers.json，无法区分 revision：{e}")
        return []

    found: list[tuple[Path, int]] = []
    for child in sorted(registry.iterdir()):
        if child.is_dir() and child.name in revisions:
            found.append((child, _dir_size(child)))
    return found


def _browser_root(component: Component) -> Path | None:
    if component.extra == "camoufox":
        base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
        if sys.platform == "win32":
            base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        elif sys.platform == "darwin":
            base = str(Path.home() / "Library" / "Caches")
        return Path(base) / "camoufox"
    if component.extra in ("playwright", "patchright"):
        from ipclick.adapters.browser_engines import playwright_registry_dir

        return playwright_registry_dir(component.extra)
    return None


def _dir_size(root: Path) -> int:
    total = 0
    try:
        for current, _dirs, files in os.walk(root):
            for name in files:
                path = os.path.join(current, name)
                try:
                    total += os.path.getsize(path)
                except OSError:
                    continue
    except OSError:
        return total
    return total


__all__ = [
    "InstallManager",
    "InstallOp",
    "Job",
    "Plan",
    "Toolchain",
    "browser_body_location",
    "detect_toolchain",
    "execute",
    "manual_hint",
    "plan",
]
