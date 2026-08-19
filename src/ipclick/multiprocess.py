from __future__ import annotations

from collections.abc import Callable
import contextlib
import os
import signal
import socket
from types import FrameType

from ipclick.exceptions import ConfigError
from ipclick.server_settings import ServerSettings, fork_supported
from ipclick.utils.log_util import log


WILDCARD_HOSTS: frozenset[str] = frozenset({"", "*", "[::]", "::"})

FORWARDED_SIGNALS: tuple[signal.Signals, ...] = (signal.SIGINT, signal.SIGTERM)

WORKER_FAILURE_EXIT_CODE = 1


def fork() -> int:
    if not fork_supported():
        raise ConfigError("多进程模式（[SERVER].processes > 1）依赖 os.fork，Windows 不支持；请把 processes 设为 1")
    return os.fork()


def probe_port(host: str, port: int) -> None:
    family = socket.AF_INET6 if ":" in host.strip("[]") or host in ("[::]", "::") else socket.AF_INET
    bind_host = host.strip("[]") if family is socket.AF_INET6 else host
    if bind_host in WILDCARD_HOSTS:
        bind_host = "::" if family is socket.AF_INET6 else "0.0.0.0"

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
    probe_port(endpoint.host, endpoint.port)

    children = [_spawn(index, worker) for index in range(processes)]
    log.info(f"IPClick 多进程模式：{processes} 个 worker 共享 {endpoint.listen_addr}（SO_REUSEPORT）")

    _forward_signals_to(children)
    for pid in children:
        with contextlib.suppress(ChildProcessError, InterruptedError):
            _ = os.waitpid(pid, 0)


def _spawn(index: int, worker: Callable[[int], None]) -> int:
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


def _forward_signals_to(children: list[int]) -> None:
    def forward(signum: int, _frame: FrameType | None) -> None:
        for pid in children:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signum)

    for received in FORWARDED_SIGNALS:
        _ = signal.signal(received, forward)


__all__ = ["FORWARDED_SIGNALS", "WILDCARD_HOSTS", "fork", "probe_port", "run_workers"]
