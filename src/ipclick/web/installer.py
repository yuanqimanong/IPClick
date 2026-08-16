"""从 Web 端安装 / 卸载可选组件。

0.3 的注释写着"本页**不安装**任何东西——装依赖要在机器上执行命令，那是网页最不
该有的能力"。0.4 明确推翻这个约束：一个已经能改配置、能代发任意请求的管理端，
再加上"装它自己声明过的那几个可选依赖"并没有实质性地扩大攻击面，而省掉的
"开一个终端 → 找到那个 venv → 敲对命令 → 重启进程"是真实的痛点。

推翻约束不等于放松要求。这个模块按下面六条写：

1. **包名全部来自白名单常量，绝不拼接用户输入。** 表单里传来的 extra 必须能在
   :data:`ipclick.components.BY_EXTRA` 里查到，查不到直接拒绝。命令用列表形式
   交给 :mod:`subprocess`（``shell=False``），连"引号转义写错"这个类别都不存在。
2. **绑定 :data:`sys.executable`。** 不依赖 PATH 上的 ``pip``，也不依赖"当前激活
   的 venv"。装到别的环境去比装不上更糟——装完 Web 端还是看不到，人会以为是
   这个功能坏了。``uv pip`` 同理，显式带 ``--python``。
3. **``pip`` 和 ``uv pip`` 两条路都支持，自动探测。** uv 创建的 venv 默认**不装
   pip**，此时 ``python -m pip`` 会报 ``No module named pip``；反过来非 uv 环境里
   又没有 ``uv`` 命令。两者都没有时明确报错并给出手动命令，绝不静默失败。
4. **长任务不占 HTTP 请求。** ``camoufox fetch`` 要下约 1 GB。跑在后台线程里，
   页面轮询状态。否则请求超时，用户以为失败，然后重复点击叠加下载。
5. **错误原样透出。** 系统 Python 下没有写权限时 pip 会失败，那条 ``Permission
   denied`` 本身就是答案，笼统的"安装失败"等于把答案藏起来。
6. **卸载只卸 Python 包。** ``pip uninstall camoufox`` 不会删掉 ``~/.cache/camoufox``
   里那 1 GB 浏览器本体。这里不替用户删——从网页上递归删一个 GB 级目录是不可逆
   操作，风险和收益完全不成比例。界面上把路径和体积摆出来，让人自己决定。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import os
from pathlib import Path
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

#: 单个任务的输出最多留多少行。``camoufox fetch`` 的进度条能刷出上万行，
#: 全留着既没用又占内存；出错时真正有用的信息总在最后。
_OUTPUT_LINES = 300

#: 任务超时（秒）。camoufox 的本体约 1 GB，慢网络下十几分钟是正常的；
#: 但也不能没有上限——一个卡死的子进程会一直占着线程和一把锁。
_TIMEOUT = 45 * 60

#: 完成的任务保留多久（秒）。页面轮询要能取到结果，但没必要留一整天。
_RETAIN = 30 * 60


@final
@dataclass
class Job:
    """一次安装 / 卸载的执行记录。"""

    id: str
    title: str
    command: tuple[str, ...]
    status: JobStatus = "running"
    returncode: int | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    _output: deque[str] = field(default_factory=lambda: deque(maxlen=_OUTPUT_LINES))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def append(self, line: str) -> None:
        with self._lock:
            self._output.append(line)

    def output(self) -> list[str]:
        with self._lock:
            return list(self._output)

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            # 命令原样展示：用户要能核对"到底往哪个环境装了什么"，
            # 也方便他复制到终端里自己重跑一遍。
            "command": " ".join(self.command),
            "status": self.status,
            "returncode": self.returncode,
            "elapsed": int((self.finished_at or time.time()) - self.started_at),
            "output": self.output(),
        }


@final
@dataclass(frozen=True)
class Toolchain:
    """用哪条命令装包。"""

    kind: Literal["pip", "uv"]
    #: 可执行部分，如 ``(python, -m, pip)`` 或 ``(uv, pip)``
    executable: tuple[str, ...]

    def command(self, verb: str, *args: str) -> tuple[str, ...]:
        """拼一条完整命令。

        ``--python`` 的位置两边不一样，这是必须区分对待的地方：``uv`` 把它当成
        **子命令的选项**（``uv pip install --python X pkg`` 才对，写成
        ``uv pip --python X install pkg`` 会直接报 usage 错误），而 ``pip`` 本来
        就跑在目标解释器里，不需要这个参数。
        """
        if self.kind == "uv":
            return (*self.executable, verb, "--python", sys.executable, *args)
        return (*self.executable, verb, *args)

    def describe(self) -> str:
        return f"{self.kind}（{' '.join(self.executable)} → {sys.executable}）"


def detect_toolchain() -> Toolchain | None:
    """探测本环境能用哪个包管理器。探测顺序是有讲究的。

    **先 pip 后 uv**：装了 pip 的环境里 ``python -m pip`` 一定作用于
    :data:`sys.executable` 这个解释器，语义最确定。``uv pip --python`` 也能指定
    解释器，但它是"从外面操作一个环境"，多一层可能出错的地方。

    两者都没有时返回 None —— uv 创建的 venv 默认不装 pip，而机器上又可能没有
    ``uv`` 命令，这个组合是真实存在的（本项目自己的开发环境就是），调用方要能
    给出手动安装命令而不是干瞪眼。
    """
    if module_probe.installed("pip"):
        return Toolchain(kind="pip", executable=(sys.executable, "-m", "pip"))

    uv = shutil.which("uv")
    if uv:
        return Toolchain(kind="uv", executable=(uv, "pip"))
    return None


def manual_hint(component: Component) -> str:
    """两条路都不通时给的手动命令。"""
    return (
        f"本环境既没有 pip 也没有 uv，无法从页面安装。请在装了其中之一的环境里执行："
        f'{sys.executable} -m pip install "ipclick[{component.extra}]"'
        f'，或 uv pip install --python {sys.executable} "ipclick[{component.extra}]"'
    )


def extra_requirements(extra: str) -> tuple[str, ...]:
    """一个 extra 展开成的依赖列表，取自**本机已装的 ipclick 自己的元数据**。

    为什么不直接装 ``ipclick[<extra>]``：那会把 ipclick 本身也拉进这次解析。
    后果有两种，都不能接受——

    * 不钉版本，包管理器就可能"升级 ipclick 来满足需求"，从索引拉一份覆盖掉
      正在运行的这个安装（开发环境里的可编辑安装尤其容易被换掉）；
    * 钉上当前版本，那个版本又必须能在索引上找到，否则解析直接失败——本地
      开发版、私有构建、还没发布的新版本全都会卡在这里。

    改成从 ``importlib.metadata`` 读 ``Requires-Dist`` 就没有这两个问题：拿到的
    正是 ``pyproject.toml`` 里为这个 extra 声明的那几行（含版本区间），装的时候
    完全不涉及 ipclick 自身。

    元数据读不到时（源码树直接跑、没装成包）回落到 ``ipclick[extra]``，
    并由调用方在输出里说明。
    """
    import importlib.metadata

    try:
        declared = importlib.metadata.requires("ipclick") or []
    except importlib.metadata.PackageNotFoundError:
        return ()

    marker = f'extra == "{extra}"'
    found: list[str] = []
    for entry in declared:
        requirement, _, condition = entry.partition(";")
        # 标记里可能写成 extra == "x" 或 extra=='x'，两种引号都认
        normalized = condition.replace("'", '"').strip()
        if marker in normalized:
            found.append(requirement.strip())
    return tuple(found)


def _install_targets(component: Component) -> tuple[tuple[str, ...], str]:
    """这次要装什么，以及给人看的一句说明。"""
    requirements = extra_requirements(component.extra)
    if requirements:
        return requirements, ""
    from ipclick import __version__

    return (
        (f"ipclick[{component.extra}]=={__version__}",),
        f"读不到 ipclick 的依赖元数据，回落到 ipclick[{component.extra}]=={__version__}（需要该版本在索引上可获取）",
    )


class InstallManager:
    """安装任务的调度与状态。

    **同一时刻只允许一个任务**：pip 会往同一个 site-packages 里写文件，两个
    并发的安装可以互相覆盖到一半，留下一个装坏了的环境。宁可让第二次点击收到
    "已有任务在跑"。
    """

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._current: Job | None = None
        self._lock: threading.Lock = threading.Lock()
        self._seq: int = 0
        #: 任务成功后调一次，用来刷新适配器注册表与安装状态缓存。
        #: 由调用方注入，这样这个模块不必知道 registry 的存在。
        self.on_finished: Any = None

    # ------------------------------------------------------------------ #
    # 对外动作
    # ------------------------------------------------------------------ #

    def install(self, extra: str) -> tuple[bool, str]:
        """装一个 extra 的 Python 包。"""
        component = self._lookup(extra)
        if component is None:
            return False, f"未知的组件 {extra!r}"
        toolchain = detect_toolchain()
        if toolchain is None:
            return False, manual_hint(component)

        targets, note = _install_targets(component)
        command = toolchain.command("install", *targets)
        return self._start(f"安装 {component.name}", command, note=note)

    def uninstall(self, extra: str) -> tuple[bool, str]:
        """卸一个 extra 的 Python 包。**不动**浏览器本体，理由见模块开头。"""
        component = self._lookup(extra)
        if component is None:
            return False, f"未知的组件 {extra!r}"
        toolchain = detect_toolchain()
        if toolchain is None:
            return False, manual_hint(component)

        # 只卸这一个发行版。刻意不追着卸它的依赖：那些可能被别的东西共用，
        # 连坐卸掉会把环境搞坏，而这里的目的只是"让这个组件不再可用"。
        command = toolchain.command("uninstall", *_uninstall_flags(toolchain), component.distribution)
        return self._start(f"卸载 {component.name}", command)

    def fetch_browser(self, extra: str, kind: str = "chromium") -> tuple[bool, str]:
        """下载 / 安装浏览器本体。

        Args:
            kind: playwright / patchright 要装哪个内核，取自 ``[BROWSER].browser``。
                非白名单值一律回落到 chromium。
        """
        component = self._lookup(extra)
        if component is None:
            return False, f"未知的组件 {extra!r}"
        if not component.browser_command:
            return False, f"{component.name} 用的是本机已装的 Chrome，不需要下载浏览器本体"
        installed, _ = _package_state(component)
        if not installed:
            # 先装包再下本体，顺序反了必然失败，不如直接说清楚
            return False, f"请先安装 {component.name} 的 Python 包，再下载浏览器本体"

        command = _browser_command(component, kind)
        if command is None:
            return False, f"不认识 {component.name} 的浏览器本体安装方式"
        return self._start(f"下载 {component.name} 浏览器本体", command)

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #

    def current(self) -> dict[str, Any] | None:
        """正在跑的任务；没有就返回最近一个已完成的。

        页面轮询只关心"现在有没有事在发生、上一件事成了没"，一个入口就够。
        """
        with self._lock:
            self._prune_locked()
            if self._current is not None:
                return self._current.snapshot()
            if not self._jobs:
                return None
            latest = max(self._jobs.values(), key=lambda j: j.started_at)
            return latest.snapshot()

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.snapshot() if job else None

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._current is not None

    # ------------------------------------------------------------------ #
    # 执行
    # ------------------------------------------------------------------ #

    @staticmethod
    def _lookup(extra: str) -> Component | None:
        """白名单查表。这是唯一一处把外部输入变成命令的地方，只允许精确匹配。"""
        return BY_EXTRA.get((extra or "").strip())

    def _start(self, title: str, command: tuple[str, ...], *, note: str = "") -> tuple[bool, str]:
        with self._lock:
            if self._current is not None:
                return False, f"已有任务在执行：{self._current.title}。装包不能并发，请等它跑完"
            self._seq += 1
            job = Job(id=f"job-{self._seq}", title=title, command=command)
            if note:
                job.append(f"（{note}）")
            self._jobs[job.id] = job
            self._current = job

        thread = threading.Thread(target=self._run, args=(job,), name="ipclick-install", daemon=True)
        thread.start()
        log.info(f"Web 端开始执行：{title} -> {' '.join(command)}")
        return True, f"{title} 已在后台开始"

    def _run(self, job: Job) -> None:
        job.append(f"$ {' '.join(job.command)}")
        try:
            process = subprocess.Popen(  # 命令来自白名单常量，shell=False
                job.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                # 关掉 pip 的进度条：它靠回车刷新同一行，收进管道后会变成
                # 几千行几乎相同的噪音，把真正的错误挤出保留窗口。
                env={**os.environ, "PIP_PROGRESS_BAR": "off", "PYTHONUNBUFFERED": "1"},
            )
        except OSError as e:
            # 命令根本起不来（找不到可执行文件、没有执行权限）——原样透出
            job.append(f"无法执行命令：{e}")
            self._finish(job, returncode=-1)
            return

        try:
            assert process.stdout is not None
            for line in process.stdout:
                job.append(line.rstrip("\n"))
            returncode = process.wait(timeout=_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            job.append(f"超时（{_TIMEOUT // 60} 分钟）已终止。网络慢的话请在终端里手动执行上面那条命令")
            returncode = -1
        except Exception as e:  # pragma: no cover - 兜底，绝不让线程带着异常退出
            job.append(f"执行出错：{type(e).__name__}: {e}")
            returncode = -1

        self._finish(job, returncode=returncode)

    def _finish(self, job: Job, *, returncode: int) -> None:
        job.returncode = returncode
        job.status = "succeeded" if returncode == 0 else "failed"
        job.finished_at = time.time()
        with self._lock:
            if self._current is job:
                self._current = None

        level = log.info if job.status == "succeeded" else log.warning
        level(f"Web 端任务结束：{job.title} -> {job.status}（退出码 {returncode}）")

        # 装完/卸完立刻刷新状态，用户不用再去点一次「刷新状态」。
        # 失败时也刷：可能是装了一半，此时展示的必须是磁盘上的真实情况。
        callback = self.on_finished
        if callback is not None:
            try:
                callback(job)
            except Exception as e:  # pragma: no cover - 回调出错不该影响任务结果
                log.warning(f"安装任务的回调失败：{e}")

    def _prune_locked(self) -> None:
        cutoff = time.time() - _RETAIN
        for key in [k for k, v in self._jobs.items() if v.finished_at and v.finished_at < cutoff]:
            del self._jobs[key]


def _uninstall_flags(toolchain: Toolchain) -> tuple[str, ...]:
    """卸载要跳过确认。``pip`` 用 ``-y``；``uv pip uninstall`` 本来就不问，
    给它加 ``-y`` 会被当成未知参数直接报错。
    """
    return ("-y",) if toolchain.kind == "pip" else ()


def _package_state(component: Component) -> tuple[bool, str | None]:
    from ipclick.components import package_status

    return package_status(component)


#: playwright / patchright 能装的内核。用白名单是因为这个值最终会进命令行——
#: 它来自 ``[BROWSER].browser`` 配置，而配置是可以从 Web 端改的。
_BROWSER_KINDS = frozenset({"chromium", "firefox", "webkit"})


def _browser_command(component: Component, kind: str = "chromium") -> tuple[str, ...] | None:
    """浏览器本体的安装命令。

    一律走 ``sys.executable -m <模块>`` 而不是 PATH 上的 ``camoufox`` /
    ``playwright`` 脚本：那些脚本属于"某个"环境，而我们要装的是**这个**环境。
    两者不一致时浏览器会下到别处去，症状是"明明下过了却还说没就绪"。
    """
    if component.extra == "camoufox":
        # camoufox 只有 Firefox 一个内核，也就没有"装哪个"这回事
        return (sys.executable, "-m", "camoufox", "fetch")
    if component.extra in ("playwright", "patchright"):
        target = kind if kind in _BROWSER_KINDS else "chromium"
        return (sys.executable, "-m", component.extra, "install", target)
    return None


def browser_body_location(component: Component) -> tuple[str, int]:
    """浏览器本体在哪、占多大（字节）。查不出来返回 ``("", 0)``。

    卸载只卸 Python 包，那 1 GB 还在磁盘上。不把这两个数字摆出来的话，用户会
    以为空间已经释放了——这正是 P0-1 里点名要说清楚的事。
    """
    root = _browser_root(component)
    if root is None or not root.exists():
        return "", 0
    return str(root), _dir_size(root)


def _browser_root(component: Component) -> Path | None:
    if component.extra == "camoufox":
        # 不调 camoufox 自己的 camoufox_path()：它默认 download_if_missing=True，
        # 连"查一下装没装"都能让它开始下 1 GB（见 browser_engines 里的详细说明）。
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
    except OSError:  # pragma: no cover - 权限问题
        return total
    return total


__all__ = [
    "InstallManager",
    "Job",
    "Toolchain",
    "browser_body_location",
    "detect_toolchain",
    "manual_hint",
]
