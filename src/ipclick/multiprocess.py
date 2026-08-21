"""基于 ``fork`` 与 ``SO_REUSEPORT`` 的多 worker 生命周期管理。"""

from __future__ import annotations

from collections.abc import Callable
import contextlib
import os
import signal
import socket
import threading
from types import FrameType

from ipclick.exceptions import ConfigError
from ipclick.server_settings import ServerSettings, fork_supported
from ipclick.utils.log_util import log


WILDCARD_HOSTS: frozenset[str] = frozenset({"", "*", "[::]", "::"})

FORWARDED_SIGNALS: tuple[signal.Signals, ...] = (signal.SIGINT, signal.SIGTERM)

WORKER_FAILURE_EXIT_CODE = 1


def fork() -> int:
    """创建 worker 子进程；不支持 fork 的平台给出明确配置错误。"""
    if not fork_supported():
        raise ConfigError("多进程模式（[SERVER].processes > 1）依赖 os.fork，Windows 不支持；请把 processes 设为 1")
    return os.fork()


def probe_port(host: str, port: int) -> None:
    """在启用端口复用前独占探测监听地址，避免掩盖真实端口冲突。"""
    family = socket.AF_INET6 if ":" in host.strip("[]") or host in ("[::]", "::") else socket.AF_INET
    bind_host = host.strip("[]") if family == socket.AF_INET6 else host
    if bind_host in WILDCARD_HOSTS:
        bind_host = "::" if family == socket.AF_INET6 else "0.0.0.0"

    probe = socket.socket(family, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((bind_host, port))
    except OSError as e:
        raise RuntimeError(
            f"端口 {host}:{port} 无法绑定（{e}）。多进程模式会打开 SO_REUSEPORT，"
            f"那会让端口冲突变成静默成功，所以这里先独占探测一次"
        ) from e
    finally:
        probe.close()


def run_workers(processes: int, endpoint: ServerSettings, worker: Callable[[int], None]) -> None:
    """启动、监控并统一终止共享端口的 worker 进程。"""
    probe_port(endpoint.host, endpoint.port)

    # 逐个 spawn 而不是列表推导：中途 fork 失败（EAGAIN、内存不够、被 cgroup pids
    # 限制挡住）时，已经起来的 worker 必须先收掉。否则父进程带着异常退出，
    # 那些 worker 变成没人管的孤儿，还靠 SO_REUSEPORT 继续占着端口对外服务——
    # 表现是"启动报错了，但端口还通，而且改了配置也没反应"。
    children: list[int] = []
    try:
        for index in range(processes):
            children.append(_spawn(index, worker))
    except BaseException:
        if children:
            log.error(f"启动第 {len(children) + 1} 个 worker 失败，正在收掉已启动的 {len(children)} 个")
            _shutdown_children(children)
        raise

    log.info(f"IPClick 多进程模式：{processes} 个 worker 共享 {endpoint.listen_addr}（SO_REUSEPORT）")

    shutdown_requested = _forward_signals_to(children)
    remaining = set(children)
    unexpected: tuple[int, int] | None = None

    # waitpid(-1) 能及时发现任意 worker 退出；按创建顺序等待会被仍存活的
    # 第一个 worker 卡住，从而永远看不到后续 worker 已崩溃。
    while remaining:
        try:
            pid, status = os.waitpid(-1, 0)
        except InterruptedError:
            continue
        except ChildProcessError:
            break
        remaining.discard(pid)

        if not shutdown_requested.is_set() and unexpected is None:
            unexpected = (pid, status)
            for sibling in remaining:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(sibling, signal.SIGTERM)

    if unexpected is not None:
        pid, status = unexpected
        if os.WIFEXITED(status):
            detail = f"exit={os.WEXITSTATUS(status)}"
        elif os.WIFSIGNALED(status):
            detail = f"signal={os.WTERMSIG(status)}"
        else:
            detail = f"status={status}"
        raise RuntimeError(f"worker pid={pid} 意外退出（{detail}），已终止其余 worker")


def _spawn(index: int, worker: Callable[[int], None]) -> int:
    """fork 单个 worker，并把未处理异常转换为稳定退出码。"""
    pid = fork()
    if pid != 0:
        return pid

    try:
        worker(index)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log.exception(f"worker {index} 退出: {e}")
        os._exit(WORKER_FAILURE_EXIT_CODE)
    os._exit(0)


def _shutdown_children(children: list[int]) -> None:
    """给已启动的 worker 发 SIGTERM 并回收，避免留下占着端口的孤儿进程。"""
    for pid in children:
        with contextlib.suppress(ProcessLookupError, OSError):
            os.kill(pid, signal.SIGTERM)
    for pid in children:
        with contextlib.suppress(ChildProcessError, OSError):
            _ = os.waitpid(pid, 0)


def _forward_signals_to(children: list[int]) -> threading.Event:
    """把父进程停机信号广播给所有 worker，并返回停机标记。"""
    shutdown_requested = threading.Event()

    def forward(signum: int, _frame: FrameType | None) -> None:
        shutdown_requested.set()
        for pid in children:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signum)

    for received in FORWARDED_SIGNALS:
        _ = signal.signal(received, forward)
    return shutdown_requested


__all__ = ["FORWARDED_SIGNALS", "WILDCARD_HOSTS", "fork", "probe_port", "run_workers"]
