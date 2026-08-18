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
from collections.abc import Callable, Iterator
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

#: 单个任务的输出最多留多少行。``camoufox fetch`` 的进度条能刷出上万行，
#: 全留着既没用又占内存；出错时真正有用的信息总在最后。
_OUTPUT_LINES = 300

#: 任务超时（秒）。camoufox 的本体约 1 GB，慢网络下十几分钟是正常的；
#: 但也不能没有上限——一个卡死的子进程会一直占着线程和一把锁。
_TIMEOUT = 45 * 60

#: 完成的任务保留多久（秒）。页面轮询要能取到结果，但没必要留一整天。
_RETAIN = 30 * 60

#: 目录大小的采样间隔（秒）。1 秒够让数字看起来在动，又不至于把一个几万文件的
#: 目录反复 walk 到成为负担（实测 ms 级）。
_SAMPLE_INTERVAL = 1.0


@final
@dataclass
class Progress:
    """一次任务的进度。

    两个来源，都可能缺席，所以分开存：

    * **子进程自己报的百分比**（``percent``）。``camoufox fetch`` 用 rich、
      ``playwright install`` 自带进度条，两者都要看得见终端才肯输出——见
      :func:`execute` 里的 ``FORCE_COLOR``。解析失败就是 None，不猜。
    * **目标目录的实际大小**（``done_bytes``）。由采样线程量出来，不依赖子进程
      配合，任何情况下都能证明"在干活"。

    ``camoufox fetch`` 那 1 GB 要下十几分钟，期间没有任何输出——用户看到的和
    进程卡死一模一样。这个类存在的全部理由就是把那两种情况区分开。
    """

    #: 0–100；量不出来时为 None（此时前端画不确定态的条）
    percent: float | None = None
    #: 目标目录当前有多大（字节）
    done_bytes: int = 0
    #: 最近一次采样算出的速度（字节/秒）
    speed: float = 0.0
    #: 给人看的一句话，如 "下载中" / "解压中"
    phase: str = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "percent": round(self.percent, 1) if self.percent is not None else None,
            "done_bytes": self.done_bytes,
            "speed": round(self.speed),
            "phase": self.phase,
        }


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
    #: 进度。装包类任务量不出字节数，只会有从输出里解析到的百分比（常常也没有）。
    progress: Progress = field(default_factory=Progress)
    #: 这次任务是怎么规划出来的。采样线程靠它知道该盯哪个目录。
    #: 不进 :meth:`snapshot`——那是给页面的，里面不该有内部对象。
    plan: Plan | None = None
    _output: deque[str] = field(default_factory=lambda: deque(maxlen=_OUTPUT_LINES))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def append(self, line: str) -> None:
        """追加一行输出。

        进度行（末尾带 ``\\r`` 的那种）会**替换**上一行而不是堆积：
        ``camoufox fetch`` 一次下载能刷出上万次更新，全留着的话真正有用的报错
        会被挤出保留窗口，而那正是出问题时唯一要看的东西。
        """
        percent = _parse_percent(line)
        # 阶段名从**任意**一行取，不只是进度行。playwright 把
        # "Downloading Chromium …" 和那条 |■■■| 进度条分成两行输出，只看进度行
        # 的话永远读不到阶段。
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
            "progress": self.progress.snapshot(),
            "output": self.output(),
        }


#: 从一行输出里抠百分比。
#:
#: 覆盖 rich（``━━━ 45.2%``）、pip（``45%``）、playwright（``|████ | 45% of 150 MiB``）
#: 这几种写法——它们都把数字紧挨着一个 ``%``。要求前面是空白或方框类字符，是为了
#: 不把 URL 里的 ``%E8`` 之类转义序列误当成进度。
_PERCENT_RE = re.compile(r"(?:^|[\s|\[（(━█▉▊▋▌▍▎▏]) ?(\d{1,3}(?:\.\d+)?)\s?%")

#: ``512.0/1.1 GB`` 这种"已完成/总量"。
#:
#: camoufox 的进度条**不显示百分比**——它用 rich 的 ``DownloadColumn``，渲染出来是
#: ``0.4/1.0 kB``。只认 ``%`` 的话，那 1 GB 下载全程都解析不到任何进度，而它恰恰是
#: 最需要进度的那一个。
_RATIO_RE = re.compile(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*([KMGT]?i?B)\b", re.IGNORECASE)

#: 剥掉 ANSI 转义序列（含 CSI 与 OSC）。给子进程 FORCE_COLOR 之后输出里全是这些。
_ANSI_RE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")


def _parse_percent(line: str) -> float | None:
    """从一行输出里估出进度百分比。

    两种写法都认：显式的 ``45%``（playwright、pip），以及 ``512.0/1.1 GB`` 这种
    分子分母（camoufox 用的 rich ``DownloadColumn`` 只给这个）。
    """
    matches = _PERCENT_RE.findall(line)
    if matches:
        try:
            value = float(matches[-1])
        except ValueError:  # pragma: no cover - 正则已经保证是数字
            return None
        return value if 0 <= value <= 100 else None

    ratio = _RATIO_RE.search(line)
    if ratio is None:
        return None
    try:
        done, total = float(ratio.group(1)), float(ratio.group(2))
    except ValueError:  # pragma: no cover
        return None
    # 单位只出现在末尾（rich 两边共用一个单位），所以直接比数值即可
    if total <= 0 or done < 0 or done > total:
        return None
    return done / total * 100


#: 进度行里那个阶段名 -> 给人看的说法。
#:
#: camoufox 下 1 GB 之后还要**解压** 1 GB，后者同样要几分钟。两段都只显示"进行中"
#: 的话，用户会以为进度条走到 100% 又回到 0% 是出了什么问题。
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
    """去掉 ANSI 控制序列。"""
    return _ANSI_RE.sub("", text)


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


# --------------------------------------------------------------------------- #
# 命令规划
# --------------------------------------------------------------------------- #

#: 支持的动作。
InstallOp = Literal["install", "uninstall", "browser"]


@final
@dataclass(frozen=True)
class Plan:
    """一次装 / 卸要执行的东西。

    "决定跑哪条命令"和"怎么跑它"分开，是因为这两件事有**两个**调用方而它们只在
    后半段不同：Web 端要后台跑 + 轮询状态，``ipclick component install`` 要当场
    跑完并把退出码交给调用者（AI 或 shell）。规划逻辑（白名单查表、工具链探测、
    依赖展开、先后顺序检查）两边必须完全一致——那正是安全边界所在。
    """

    op: InstallOp
    component: Component
    title: str
    command: tuple[str, ...]
    note: str = ""

    @property
    def shell_form(self) -> str:
        """给人看的一行命令。仅用于展示——真正执行时走的是 :attr:`command` 列表。"""
        return " ".join(self.command)


def plan(op: str, extra: str, *, browser_kind: str = "chromium") -> tuple[Plan | None, str]:
    """把 ``(动作, 组件)`` 解析成一条待执行的命令。

    Returns:
        ``(计划, 说明)``。计划为 None 时说明就是拒绝的理由——它总是可读的、
        并尽量带上下一步该干什么。
    """
    component = BY_EXTRA.get((extra or "").strip())
    if component is None:
        known = ", ".join(sorted(BY_EXTRA))
        return None, f"未知的组件 {extra!r}。可选：{known}"

    if op == "browser":
        if not component.browser_command:
            return None, f"{component.name} 用的是本机已装的 Chrome，不需要下载浏览器本体"
        installed, _ = _package_state(component)
        if not installed:
            # 先装包再下本体，顺序反了必然失败，不如直接说清楚
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

    # 只卸这一个发行版。刻意不追着卸它的依赖：那些可能被别的东西共用，
    # 连坐卸掉会把环境搞坏，而这里的目的只是"让这个组件不再可用"。
    command = toolchain.command("uninstall", *_uninstall_flags(toolchain), component.distribution)
    return Plan("uninstall", component, f"卸载 {component.name}", command), ""


def _child_env() -> dict[str, str]:
    """子进程的环境。

    ``FORCE_COLOR=1`` 是关键的一项：``camoufox fetch`` 的进度条用 rich 画，而
    rich 检测到 stdout 不是终端就**不再实时刷新**——收进管道之后我们只能看到首尾
    两段，中间十几分钟一片空白。rich 认 ``FORCE_COLOR``，于是它照常按终端输出，
    我们再自己剥掉 ANSI 序列。``COLUMNS`` 定死宽度，免得它按 80 列之外的什么值排版。

    ``PIP_PROGRESS_BAR=off`` 方向相反，也是对的：pip 的进度条没有任何有用信息
    （装包的耗时在解析依赖上，不在下载上），而它会刷出几千行几乎相同的噪音。
    """
    return {
        **os.environ,
        "PIP_PROGRESS_BAR": "off",
        "PYTHONUNBUFFERED": "1",
        "FORCE_COLOR": "1",
        "COLUMNS": "100",
    }


def _iter_progress_lines(stream: Any) -> Iterator[str]:
    """按 ``\\n`` **和** ``\\r`` 切分子进程的输出。

    进度条是靠回车把光标拉回行首、原地重画实现的，所以它整段下载只产生**一行**
    （中间全是 ``\\r``）。按 ``\\n`` 读的话，那一行要等下载彻底结束才会吐出来——
    正是最需要看到进度的那十几分钟里什么都收不到。
    """
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
    """跑一条命令，逐行回调输出，返回退出码。

    绝不抛异常：连命令起不来（找不到可执行文件）和超时都变成一行输出 + 退出码
    ``-1``。装包失败是常态（网络、权限、解析冲突），调用方要的是那条真实的报错，
    而不是一个 traceback。
    """
    on_line(f"$ {' '.join(command)}")
    try:
        process = subprocess.Popen(  # 命令来自白名单常量，shell=False
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_child_env(),
        )
    except OSError as e:
        # 命令根本起不来（找不到可执行文件、没有执行权限）——原样透出
        on_line(f"无法执行命令：{e}")
        return -1

    try:
        assert process.stdout is not None
        for line in _iter_progress_lines(process.stdout):
            on_line(strip_ansi(line).rstrip())
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        on_line(f"超时（{timeout // 60} 分钟）已终止。网络慢的话请在终端里手动执行上面那条命令")
        return -1
    except Exception as e:  # pragma: no cover - 兜底，绝不让调用方带着异常退出
        on_line(f"执行出错：{type(e).__name__}: {e}")
        return -1


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
        return self._plan_and_start("install", extra)

    def uninstall(self, extra: str) -> tuple[bool, str]:
        """卸一个 extra 的 Python 包。**不动**浏览器本体，理由见模块开头。"""
        return self._plan_and_start("uninstall", extra)

    def fetch_browser(self, extra: str, kind: str = "chromium") -> tuple[bool, str]:
        """下载 / 安装浏览器本体。

        Args:
            kind: playwright / patchright 要装哪个内核，取自 ``[BROWSER].browser``。
                非白名单值一律回落到 chromium。
        """
        return self._plan_and_start("browser", extra, browser_kind=kind)

    def _plan_and_start(self, op: InstallOp, extra: str, *, browser_kind: str = "chromium") -> tuple[bool, str]:
        prepared, reason = plan(op, extra, browser_kind=browser_kind)
        if prepared is None:
            return False, reason
        return self._start(prepared)

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
        """盯着目标目录长多大，作为进度的兜底来源。

        子进程报不报进度不由我们说了算（rich 的行为、playwright 的版本、pip 的
        开关都可能变），但"磁盘上多了多少字节"永远量得到。有了它，用户至少能看到
        数字在动——而"卡住了没有"正是这十几分钟里唯一想知道的事。

        只对**下载浏览器本体**这类任务开：装 Python 包写的是 site-packages，那个
        目录里本来就有别的东西，量出来的增量没有意义。
        """
        if job.plan is None or job.plan.op != "browser":
            return None
        root = _browser_root(job.plan.component)
        if root is None:
            return None

        # 基线在**起线程之前**量，不是在线程里量。
        #
        # 报的是"这次任务写了多少"而不是目录总大小：playwright 与 patchright 共用
        # ms-playwright，里面可能已经躺着另一个的 600 MB，报总量的话 patchright
        # 刚开始下就显示"已写入 638 MB"——那个数字比它要下的东西还大。
        #
        # 而基线必须早于子进程的第一个字节。放在线程里量的话，线程被调度得晚一点
        # （机器忙、GIL 争用）就会把子进程已经写下去的部分算进基线，于是进度永远
        # 显示 0。这个竞态在真实下载里罕见（要几分钟），却正因为罕见才难查。
        baseline = _dir_size(root)

        def sample() -> None:
            last_size, last_at = baseline, time.monotonic()
            while not stop.wait(_SAMPLE_INTERVAL):
                size = _dir_size(root)
                now = time.monotonic()
                elapsed = max(0.001, now - last_at)
                job.progress.done_bytes = max(0, size - baseline)
                # 速度按两次采样之间的增量算。负数（解压完删临时文件）当 0——
                # 界面上出现一个负速度只会让人以为读错了。
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

    playwright / patchright **共用** ``ms-playwright`` 这个注册表目录，所以对它们
    报整个目录的大小是错的（两个组件会显示同一个数）。这两个走
    :func:`playwright_revisions`，只算自己那几个 revision 目录。
    """
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
    """``engine`` 这一份实际占用的 revision 目录与各自体积。

    版本号取自驱动自带的 ``browsers.json``——那是**它自己**下载时用的那个号，
    比在目录里按前缀猜准得多。playwright 与 patchright 钉的号不同（实测
    chromium-1223 vs chromium-1228），所以同一个 ms-playwright 目录里躺着两份
    互不相干的构建；页面上要说清这件事，就得能分别算出各自占了多少。
    """
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
            # 清单里写 chromium-headless-shell，磁盘上却是
            # chromium_headless_shell-1223 —— 目录名把名字里的连字符换成了下划线。
            # 两种拼法都收，漏掉的后果是那一份体积算不进去（页面上显示得偏小）。
            revisions.add(f"{name}-{revision}")
            revisions.add(f"{name.replace('-', '_')}-{revision}")
    except Exception as e:  # 包没装、清单格式变了、读不了
        log.debug(f"读不到 {engine} 的 browsers.json，无法区分 revision：{e}")
        return []

    found: list[tuple[Path, int]] = []
    for child in sorted(registry.iterdir()):
        if child.is_dir() and child.name in revisions:
            found.append((child, _dir_size(child)))
    return found


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
